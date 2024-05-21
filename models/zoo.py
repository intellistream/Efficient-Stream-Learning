import torch
import torch.nn as nn
from .vit import VisionTransformer
import copy
from timm.models import create_model
from .moe import MoEModule

# Our method!
class CodaPrompt(nn.Module):
    def __init__(self, emb_d, n_tasks, prompt_param, prompt_attune, key_dim=768):
        super().__init__()
        self.task_count = 0
        self.emb_d = emb_d
        self.key_d = key_dim
        self.n_tasks = n_tasks
        self.prompt_attune = prompt_attune
        self._init_smart(emb_d, prompt_param)

        # e prompt init
        for e in self.e_layers:
            # for model saving/loading simplicity, we init the full paramaters here
            # however, please note that we reinit the new components at each task
            # in the "spirit of continual learning", as we don't know how many tasks
            # we will encounter at the start of the task sequence
            #
            # in the original paper, we used ortho init at the start - this modification is more
            # fair in the spirit of continual learning and has little affect on performance
            e_l = self.e_p_length
            p = tensor_prompt(self.e_pool_size, e_l, emb_d)
            k = tensor_prompt(self.e_pool_size, self.key_d)
            a = tensor_prompt(self.e_pool_size, self.key_d)
            p = self.gram_schmidt(p)
            k = self.gram_schmidt(k)
            a = self.gram_schmidt(a)
            setattr(self, f'e_p_{e}',p)
            setattr(self, f'e_k_{e}',k)
            setattr(self, f'e_a_{e}',a)
        if self.prompt_attune:
            moe_layer = MoEModule(num_new_experts=0,
                                 # input_dim=self.emb_d * self.e_p_length,
                                 input_dim=self.emb_d,
                                 hidden_dim=self.emb_d,
                                 old_expert_blocks=[9, 10, 11])
            # setattr(self, f'moe', moe_layer)
            setattr(self, f'moe', moe_layer)

    def _init_smart(self, emb_d, prompt_param):

        # prompt basic param
        self.e_pool_size = int(prompt_param[0])
        self.e_p_length = int(prompt_param[1])
        self.e_layers = [0,1,2,3,4]

        # strenth of ortho penalty
        self.ortho_mu = prompt_param[2]

    def process_task_count(self):
        self.task_count += 1

        # in the spirit of continual learning, we will reinit the new components
        # for the new task with Gram Schmidt
        #
        # in the original paper, we used ortho init at the start - this modification is more
        # fair in the spirit of continual learning and has little affect on performance
        #
        # code for this function is modified from:
        # https://github.com/legendongary/pytorch-gram-schmidt/blob/master/gram_schmidt.py
        for e in self.e_layers:
            K = getattr(self,f'e_k_{e}')
            A = getattr(self,f'e_a_{e}')
            P = getattr(self,f'e_p_{e}')
            k = self.gram_schmidt(K)
            a = self.gram_schmidt(A)
            p = self.gram_schmidt(P)
            setattr(self, f'e_p_{e}',p)
            setattr(self, f'e_k_{e}',k)
            setattr(self, f'e_a_{e}',a)

    def process_batch_init(self):
        for e in self.e_layers:
            K = getattr(self,f'e_k_{e}')
            A = getattr(self,f'e_a_{e}')
            P = getattr(self,f'e_p_{e}')
            k = self.gram_schmidt(K)
            a = self.gram_schmidt(A)
            p = self.gram_schmidt(P)
            setattr(self, f'e_p_{e}',p)
            setattr(self, f'e_k_{e}',k)
            setattr(self, f'e_a_{e}',a)

    # code for this function is modified from:
    # https://github.com/legendongary/pytorch-gram-schmidt/blob/master/gram_schmidt.py
    def gram_schmidt(self, vv):

        def projection(u, v):
            denominator = (u * u).sum()

            if denominator < 1e-8:
                return None
            else:
                return (v * u).sum() / denominator * u

        # check if the tensor is 3D and flatten the last two dimensions if necessary
        is_3d = len(vv.shape) == 3
        if is_3d:
            shape_2d = copy.deepcopy(vv.shape)
            vv = vv.view(vv.shape[0],-1)

        # swap rows and columns
        vv = vv.T

        # process matrix size
        nk = vv.size(1)
        uu = torch.zeros_like(vv, device=vv.device)

        # get starting point
        pt = int(self.e_pool_size / (self.n_tasks))
        s = int(self.task_count * pt)
        f = int((self.task_count + 1) * pt)
        if s > 0:
            uu[:, 0:s] = vv[:, 0:s].clone()
        for k in range(s, f):
            redo = True
            while redo:
                redo = False
                vk = torch.randn_like(vv[:,k]).to(vv.device)
                uk = 0
                for j in range(0, k):
                    if not redo:
                        uj = uu[:, j].clone()
                        proj = projection(uj, vk)
                        if proj is None:
                            redo = True
                            print('restarting!!!')
                        else:
                            uk = uk + proj
                if not redo: uu[:, k] = vk - uk
        for k in range(s, f):
            uk = uu[:, k].clone()
            uu[:, k] = uk / (uk.norm())

        # undo swapping of rows and columns
        uu = uu.T

        # return from 2D
        if is_3d:
            uu = uu.view(shape_2d)

        return torch.nn.Parameter(uu)

    def forward(self, x_querry, l, x_block, train=False, task_id=None):

        # e prompts
        e_valid = False
        if l in self.e_layers:
            e_valid = True
            B, C = x_querry.shape

            K = getattr(self,f'e_k_{l}')
            A = getattr(self,f'e_a_{l}')
            p = getattr(self,f'e_p_{l}')
            pt = int(self.e_pool_size / self.n_tasks)
            s = int(self.task_count * pt)
            f = int((self.task_count + 1) * pt)

            # freeze/control past tasks
            if train:
                if self.task_count > 0:
                    K = torch.cat((K[:s].detach().clone(),K[s:f]), dim=0)
                    A = torch.cat((A[:s].detach().clone(),A[s:f]), dim=0)
                    p = torch.cat((p[:s].detach().clone(),p[s:f]), dim=0)
                else:
                    K = K[s:f]
                    A = A[s:f]
                    p = p[s:f]
            else:
                K = K[0:f]
                A = A[0:f]
                p = p[0:f]
            if self.prompt_attune:
                p = getattr(self, f'moe')(p)
                # p = getattr(self, f'moe_{l}')(p)
            # with attention and cosine sim
            # (b x 1 x d) * soft([1 x k x d]) = (b x k x d) -> attention = k x d
            a_querry = torch.einsum('bd,kd->bkd', x_querry, A)
            # # (b x k x d) - [1 x k x d] = (b x k) -> key = k x d
            n_K = nn.functional.normalize(K, dim=1)
            q = nn.functional.normalize(a_querry, dim=2)
            aq_k = torch.einsum('bkd,kd->bk', q, n_K)
            # (b x 1 x k x 1) * [1 x plen x k x d] = (b x plen x d) -> prompt = plen x k x d
            P_ = torch.einsum('bk,kld->bld', aq_k, p)
            # print(f'x_querry.shape:{x_querry.shape}\t '
            #       f'A.shape:{A.shape}\t'
            #       f' a_querry.shape:{a_querry.shape}\t'
            #       f' aq_k.shape:{aq_k.shape}\t'
            #       f' P_.shape:{P_.shape}')


            # select prompts
            i = int(self.e_p_length/2)
            Ek = P_[:,:i,:]
            Ev = P_[:,i:,:]

            # ortho penalty
            if train and self.ortho_mu > 0:
                loss = ortho_penalty(K) * self.ortho_mu
                loss += ortho_penalty(A) * self.ortho_mu
                loss += ortho_penalty(p.view(p.shape[0], -1)) * self.ortho_mu
            else:
                loss = 0
        else:
            loss = 0

        # combine prompts for prefix tuning
        if e_valid:
            p_return = [Ek, Ev]
        else:
            p_return = None

        # return
        return p_return, loss, x_block

def ortho_penalty(t):
    return ((t @t.T - torch.eye(t.shape[0]).cuda())**2).mean()

# @article{wang2022dualprompt,
#   title={DualPrompt: Complementary Prompting for Rehearsal-free Continual Learning},
#   author={Wang, Zifeng and Zhang, Zizhao and Ebrahimi, Sayna and Sun, Ruoxi and Zhang, Han and Lee, Chen-Yu and Ren, Xiaoqi and Su, Guolong and Perot, Vincent and Dy, Jennifer and others},
#   journal={European Conference on Computer Vision},
#   year={2022}
# }
class DualPrompt(nn.Module):
    def __init__(self, emb_d, n_tasks, prompt_param, key_dim=768):
        super().__init__()
        self.task_count = 0
        self.emb_d = emb_d
        self.key_d = key_dim
        self.n_tasks = n_tasks
        self._init_smart(emb_d, prompt_param)

        # g prompt init
        for g in self.g_layers:
            p = tensor_prompt(self.g_p_length, emb_d)
            setattr(self, f'g_p_{g}',p)

        # e prompt init
        for e in self.e_layers:
            p = tensor_prompt(self.e_pool_size, self.e_p_length, emb_d)
            k = tensor_prompt(self.e_pool_size, self.key_d)
            setattr(self, f'e_p_{e}',p)
            setattr(self, f'e_k_{e}',k)

    def _init_smart(self, emb_d, prompt_param):

        self.top_k = 1
        self.task_id_bootstrap = True

        # prompt locations
        self.g_layers = [0,1]
        self.e_layers = [2,3,4]

        # prompt pool size
        self.g_p_length = int(prompt_param[2])
        self.e_p_length = int(prompt_param[1])
        self.e_pool_size = int(prompt_param[0])

    def process_task_count(self):
        self.task_count += 1

    def forward(self, x_querry, l, x_block, train=False, task_id=None):

        # e prompts
        e_valid = False
        if l in self.e_layers:
            e_valid = True
            B, C = x_querry.shape
            K = getattr(self,f'e_k_{l}') # 0 based indexing here
            p = getattr(self,f'e_p_{l}') # 0 based indexing here

            # cosine similarity to match keys/querries
            n_K = nn.functional.normalize(K, dim=1)
            q = nn.functional.normalize(x_querry, dim=1).detach()
            cos_sim = torch.einsum('bj,kj->bk', q, n_K)

            if train:
                # dual prompt during training uses task id
                if self.task_id_bootstrap:
                    loss = (1.0 - cos_sim[:,task_id]).sum()
                    P_ = p[task_id].expand(len(x_querry),-1,-1)
                else:
                    top_k = torch.topk(cos_sim, self.top_k, dim=1)
                    k_idx = top_k.indices
                    loss = (1.0 - cos_sim[:,k_idx]).sum()
                    P_ = p[k_idx]
            else:
                top_k = torch.topk(cos_sim, self.top_k, dim=1)
                k_idx = top_k.indices
                P_ = p[k_idx]

            # select prompts
            if train and self.task_id_bootstrap:
                i = int(self.e_p_length/2)
                Ek = P_[:,:i,:].reshape((B,-1,self.emb_d))
                Ev = P_[:,i:,:].reshape((B,-1,self.emb_d))
            else:
                i = int(self.e_p_length/2)
                Ek = P_[:,:,:i,:].reshape((B,-1,self.emb_d))
                Ev = P_[:,:,i:,:].reshape((B,-1,self.emb_d))

        # g prompts
        g_valid = False
        if l in self.g_layers:
            g_valid = True
            j = int(self.g_p_length/2)
            p = getattr(self,f'g_p_{l}') # 0 based indexing here
            P_ = p.expand(len(x_querry),-1,-1)
            Gk = P_[:,:j,:]
            Gv = P_[:,j:,:]

        # combine prompts for prefix tuning
        if e_valid and g_valid:
            Pk = torch.cat((Ek, Gk), dim=1)
            Pv = torch.cat((Ev, Gv), dim=1)
            p_return = [Pk, Pv]
        elif e_valid:
            p_return = [Ek, Ev]
        elif g_valid:
            p_return = [Gk, Gv]
            loss = 0
        else:
            p_return = None
            loss = 0

        # return
        if train:
            return p_return, loss, x_block
        else:
            return p_return, 0, x_block

# @inproceedings{wang2022learning,
#   title={Learning to prompt for continual learning},
#   author={Wang, Zifeng and Zhang, Zizhao and Lee, Chen-Yu and Zhang, Han and Sun, Ruoxi and Ren, Xiaoqi and Su, Guolong and Perot, Vincent and Dy, Jennifer and Pfister, Tomas},
#   booktitle={Proceedings of the IEEE/CVF Conference on Computer Vision and Pattern Recognition},
#   pages={139--149},
#   year={2022}
# }
class L2P(DualPrompt):
    def __init__(self, emb_d, n_tasks, prompt_param, key_dim=768):
        super().__init__(emb_d, n_tasks, prompt_param, key_dim)

    def _init_smart(self, emb_d, prompt_param):
        self.top_k = 5
        self.task_id_bootstrap = False

        # prompt locations
        self.g_layers = []
        if prompt_param[2] > 0:
            self.e_layers = [0,1,2,3,4]
        else:
            self.e_layers = [0]

        # prompt pool size
        self.g_p_length = -1
        self.e_p_length = int(prompt_param[1])
        self.e_pool_size = int(prompt_param[0])

# note - ortho init has not been found to help l2p/dual prompt
def tensor_prompt(a, b, c=None, ortho=False):
    if c is None:
        p = torch.nn.Parameter(torch.FloatTensor(a,b), requires_grad=True)
    else:
        p = torch.nn.Parameter(torch.FloatTensor(a,b,c), requires_grad=True)
    if ortho:
        nn.init.orthogonal_(p)
    else:
        nn.init.uniform_(p)
    return p

def load_different_vit(name="None"):
    model = VisionTransformer(img_size=224, patch_size=16, embed_dim=768, depth=12,
                              num_heads=12, ckpt_layer=0, drop_path_rate=0)
    state_dict = model.state_dict()
    if name in ['sup21k', 'ibot21k', 'ibot', 'moco', 'dino']:
        print(f"Pretrained Model: {name}")
    else:
        print(f"Pretrained Model: ImageNet1k")

    if name == 'sup21k':
        from timm.models import vit_base_patch16_224_in21k
        load_dict = vit_base_patch16_224_in21k(pretrained=True).state_dict()
    elif name == 'ibot21k':
        load_dict = torch.load("ckpt/ibot21k/checkpoint.pth", map_location='cpu')['teacher']
    elif name == 'ibot':
        load_dict = torch.load('ckpt/ibot/checkpoint_teacher.pth', map_location='cpu')['state_dict']
    elif name == 'moco':
        load_dict = torch.load('ckpt/moco/vit-b-300ep.pth.tar', map_location='cpu')
    elif name == 'dino':
        load_dict = torch.load("ckpt/dino/dino_vitbase16_pretrain.pth", map_location='cpu')
    else:
        from timm.models import vit_base_patch16_224
        load_dict = vit_base_patch16_224(pretrained=True).state_dict()
    not_in_k = [k for k in load_dict.keys() if k not in state_dict.keys()]
    for k in not_in_k:
        del load_dict[k]
    state_dict.update(load_dict)
    model.load_state_dict(state_dict)

    return model

class ViTZoo(nn.Module):
    def __init__(self, num_classes=10, pt=False, args=None, prompt_flag=None, prompt_param=None, learnloss=False):
        super(ViTZoo, self).__init__()

        # get last layer
        # self.last = nn.Linear(512, num_classes)
        self.args = args
        self.prompt_attune = bool(args.prompt_attune)
        self.prompt_flag = prompt_flag
        self.task_id = None
        self.learnloss = learnloss
        # get feature encoder
        if pt:
            print('Resume the ViT')
            zoo_model = load_different_vit(name=self.args.ptm)
            # feature encoder changes if transformer vs resnet
            self.feat = zoo_model
            # classifier
            self.last = nn.Linear(self.feat.num_features, num_classes)

        # create prompting module
        if self.prompt_flag == 'l2p':
            self.prompt = L2P(self.feat.num_features, n_tasks=prompt_param[0], prompt_param=prompt_param[1])
        elif self.prompt_flag == 'dual':
            self.prompt = DualPrompt(self.feat.num_features, n_tasks=prompt_param[0], prompt_param=prompt_param[1])
        elif self.prompt_flag == 'coda':
            print(f'Init CODA-Prompt')
            self.prompt = CodaPrompt(self.feat.num_features, n_tasks=prompt_param[0], prompt_param=prompt_param[1],
                                     prompt_attune=self.prompt_attune)
        else:
            self.prompt = None
            # raise NotImplementedError(f"Unkown prompt flag: {self.prompt_flag}")

        if self.learnloss:
            from dataselections.learnloss import LossNet
            print(f'Init LossNet for learning loss selection...')
            self.lossnet = LossNet(num_channels=self.feat.num_features)

    def features(self, x, class_output=False):
        if self.prompt is not None:
            with torch.no_grad():
                q, _, _ = self.feat(x)
                q = q[:,0,:]
            out, prompt_loss, features = self.feat(x, prompt=self.prompt, q=q, train=False, task_id=self.task_id,
                                         learnloss=self.learnloss)
            out = out[:,0,:]
        else:
            out, _, _ = self.feat(x)
            out = out[:,0,:]
        logits = out.view(out.size(0), -1)
        if class_output:
            outputs = self.last(logits)
            return outputs, logits
        return logits

    # pen: get penultimate features
    def forward(self, x, aug_x=None, pen=False, train=False, gradmatch=False, learnloss=False):

        if self.prompt is not None:
            with torch.no_grad():
                q, _, _ = self.feat(x)
                q = q[:,0,:]
            out, prompt_loss, features = self.feat(x, prompt=self.prompt, q=q, train=train, task_id=self.task_id,
                                         learnloss=self.learnloss)
            out = out[:,0,:]
            if self.learnloss:
                pred_loss = self.lossnet(features)
            else:
                pred_loss = None
            if learnloss:
                return pred_loss

        else:
            out, _, _ = self.feat(x)
            out = out[:,0,:]
        logits = out.view(out.size(0), -1)
        if not pen:
            logits = self.last(logits)
        if self.prompt is not None and train:
            return logits, prompt_loss, pred_loss
        elif self.prompt is not None and (not train):
            if gradmatch:
                return logits, out.view(out.size(0), -1)
            else:
                return logits
        else:
            return logits

def vit_pt_imnet(out_dim, args=None, prompt_flag = 'None', prompt_param=None, learnloss=False):
    return ViTZoo(num_classes=out_dim, pt=True, args=args, prompt_flag=prompt_flag,
                  prompt_param=prompt_param, learnloss=learnloss)

