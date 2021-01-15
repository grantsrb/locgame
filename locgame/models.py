import torch
import torch.nn as nn
from torch.nn import ReLU, Tanh
import numpy as np
import time
import os
import torch.nn.functional as F
from transformer.custom_modules import *
from transformer.models import *
from ml_utils.utils import update_shape

d = {i:"cuda:"+str(i) for i in range(torch.cuda.device_count())}
DEVICE_DICT = {-1:"cpu", **d}

class CustomModule:
    @property
    def is_cuda(self):
        try:
            return next(self.parameters()).is_cuda
        except:
            return False

    def get_device(self):
        try:
            return next(self.parameters()).get_device()
        except:
            return -1

class RSSM(nn.Module, CustomModule):
    def __init__(self, h_size, s_size, a_size, rnn_type="GRU",
                                               min_sigma=0.0001):
        super(RSSM, self).__init__()
        """
        h_size - int
            size of belief vector h
        s_size - int
            size of state vector s
        a_size - int
            size of action space vector a

        min_sigma - float
            the minimum value that the state standard deviation can take
        """
        if rnn_type == "GRU":
            rnn_type = "GRUCell"
        assert rnn_type == "GRUCell" # Only supported type currently
        self.h_size = h_size
        self.s_size = s_size
        self.a_size = a_size
        self.rnn_type = rnn_type
        self.min_sigma = min_sigma

        self.rnn = getattr(nn, rnn_type)(input_size=(s_size+a_size),
                                         hidden_size=h_size)
        # Creates mu and sigma for state gaussian
        self.state_layer = nn.Linear(h_size, 2*s_size) 

    def forward(self, h, s, a):
        x = torch.cat([s,a], dim=-1)
        h_new = self.rnn(x, h)
        musigma = self.state_layer(h_new)
        mu, sigma = torch.chunk(musigma, 2, dim=-1)
        sigma = F.softplus(sigma) + self.min_sigma
        return h_new, mu, sigma
    
    def extra_repr(self):
        s = "h_size={}, s_size={}, a_size={}, min_sigma={}"
        return s.format(self.h_size, self.s_size, self.a_size,
                                                  self.min_sigma)


class LocatorBase(TransformerBase, CustomModule):
    def __init__(self,obj_recog=False,rew_recog=False,
                                      n_numbers=7,
                                      n_colors=7,
                                      n_shapes=7,
                                      *args,**kwargs):
        super().__init__(*args,**kwargs)
        self.obj_recog = obj_recog
        self.rew_recog = rew_recog
        self.n_numbers = n_numbers
        self.n_colors = n_colors
        self.n_shapes = n_shapes

class TransformerLocator(LocatorBase):
    def __init__(self, cnn_type="SimpleCNN", **kwargs):
        super().__init__(**kwargs)
        self.cnn_type = cnn_type
        self.cnn = globals()[self.cnn_type](**kwargs)
        self.pos_encoder = PositionalEncoder(self.cnn.seq_len,
                                             self.emb_size)
        max_num_steps = 20
        self.extractor = Decoder(max_num_steps,
                                 emb_size=self.emb_size,
                                 attn_size=self.attn_size,
                                 n_layers=self.dec_layers,
                                 n_heads=self.n_heads,
                                 act_fxn=self.act_fxn,
                                 use_mask=False,
                                 init_decs=False,
                                 prob_embs=self.prob_embs,
                                 prob_attn=self.prob_attn)

        # Learned initialization for memory transformer init vector
        self.h_init = torch.randn(1,1,self.emb_size)
        divisor = float(np.sqrt(self.emb_size))
        self.h_init = nn.Parameter(self.h_init/divisor)

        self.locator = nn.Sequential(
            nn.LayerNorm(self.emb_size),
            nn.Linear(self.emb_size, self.class_h_size),
            globals()[self.act_fxn](),
            nn.LayerNorm(self.class_h_size),
            nn.Linear(self.class_h_size, 2),
            nn.Tanh()
        )
        # Obj recognition model
        if self.obj_recog:
            self.color = nn.Sequential(
                nn.LayerNorm(self.emb_size),
                nn.Linear(self.emb_size, self.class_h_size),
                globals()[self.act_fxn](),
                nn.LayerNorm(self.class_h_size),
                nn.Linear(self.class_h_size, self.n_colors)
            )
            self.shape = nn.Sequential(
                nn.LayerNorm(self.emb_size),
                nn.Linear(self.emb_size, self.class_h_size),
                globals()[self.act_fxn](),
                nn.LayerNorm(self.class_h_size),
                nn.Linear(self.class_h_size, self.n_shapes)
            )

    def fresh_h(self, batch_size=1):
        return self.h_init.repeat(batch_size,1,1)

    def reset_h(self, batch_size=1):
        self.h = self.fresh_h(batch_size)
        return self.h

    def forward(self, x, h=None):
        """
        x: torch float tensor (B,C,H,W)
        """
        if h is None:
            h = self.h
        feats = self.cnn(x)
        feats = self.pos_encoder(feats)
        h = self.extractor(h, feats)
        loc = self.locator(h[:,0])
        if self.obj_recog:
            color = self.color(h[:,0])
            shape = self.shape(h[:,0])
        else:
            color,shape = [],[]
        self.h = torch.cat([self.fresh_h(len(x)),h],axis=1)
        return loc,color,shape

class RNNLocator(LocatorBase):
    def __init__(self, cnn_type="SimpleCNN", rnn_type="GRUCell",
                                             aud_targs=False,
                                             fixed_h=False,
                                             countOut=0,
                                             **kwargs):
        """
        cnn_type: str
            the class of cnn to use for creating features from the image
        rnn_type: str
            the class of rnn to use for the temporal model
        aud_targs: bool
            if true, a color and shape must be specified at each step. 
            This creates two separate embeddings that are concatenated
            to the hidden state and projected down into the appropriate
            size for feature extraction and for the rnn. Stands for 
            audible targs
        fixed_h: bool
            if true, the h value is reset at each step in the episode
        countOut: bool or int
            if true, then counting out the number of objects is part of
            the process
        """
        super().__init__(**kwargs)
        self.cnn_type = cnn_type
        self.rnn_type = rnn_type
        self.aud_targs = aud_targs
        self.fixed_h = fixed_h
        self.count_out = countOut
        if self.fixed_h: print("USING FIXED H VECTOR!!")
        self.cnn = globals()[self.cnn_type](**kwargs)
        self.pos_encoder = PositionalEncoder(self.cnn.seq_len,
                                             self.emb_size)
        self.extractor = Attncoder(1, emb_size=self.emb_size,
                                 attn_size=self.attn_size,
                                 n_layers=self.dec_layers,
                                 n_heads=self.n_heads,
                                 act_fxn=self.act_fxn,
                                 use_mask=False,
                                 init_decs=False,
                                 gen_decs=False,
                                 prob_embs=self.prob_embs,
                                 prob_attn=self.prob_attn)

        # Learned initialization for rnn hidden vector
        self.h_shape = (1,self.emb_size)
        self.h_init = torch.randn(self.h_shape)
        divisor = float(np.sqrt(self.emb_size))
        self.h_init = nn.Parameter(self.h_init/divisor)

        emb_count = 1
        if self.count_out:
            emb_count += 1
            self.count_embs =nn.Embedding(self.n_numbers,self.emb_size)
        if self.aud_targs:
            emb_count += 2
            self.color_embs = nn.Embedding(self.n_colors,self.emb_size)
            self.shape_embs = nn.Embedding(self.n_shapes,self.emb_size)
        if emb_count > 1:
            self.aud_projection = nn.Linear(emb_count*self.emb_size,
                                          self.emb_size)

        self.rnn = getattr(nn,self.rnn_type)(input_size=self.emb_size,
                                             hidden_size=self.emb_size)

        self.locator = nn.Sequential(
            #nn.LayerNorm(self.emb_size),
            nn.Linear(self.emb_size, self.class_h_size),
            globals()[self.act_fxn](),
            #nn.LayerNorm(self.class_h_size),
            nn.Linear(self.class_h_size, 2),
            nn.Tanh()
        )
        # Reward model
        self.pavlov = nn.Sequential(
            #nn.LayerNorm(self.emb_size),
            nn.Linear(self.emb_size, self.class_h_size),
            globals()[self.act_fxn](),
            #nn.LayerNorm(self.class_h_size),
            nn.Linear(self.class_h_size, 1)
        )
        # Obj recognition model
        if self.obj_recog and not self.aud_targs:
            self.color = nn.Sequential(
                nn.LayerNorm(self.emb_size),
                nn.Linear(self.emb_size, self.class_h_size),
                globals()[self.act_fxn](),
                nn.LayerNorm(self.class_h_size),
                nn.Linear(self.class_h_size, self.n_colors)
            )
            self.shape = nn.Sequential(
                nn.LayerNorm(self.emb_size),
                nn.Linear(self.emb_size, self.class_h_size),
                globals()[self.act_fxn](),
                nn.LayerNorm(self.class_h_size),
                nn.Linear(self.class_h_size, self.n_shapes)
            )

    def reset_h(self, batch_size=1):
        """
        returns an h that is of shape (B,E)
        """
        self.h = self.h_init.repeat(batch_size,1)
        return self.h

    def forward(self, x, h=None, color_idx=None,
                                 shape_idx=None,
                                 count_idx=None):
        """
        x: torch float tensor (B,C,H,W)
        h: optional float tensor (B,E)
        color_idx: long tensor (B,)
        shape_idx: long tensor (B,)
        count_idx: long tensor (B,)
        """
        if h is None:
            h = self.h
        if self.fixed_h:
            h = self.reset_h(len(x))
        cat_arr = [h]
        if self.count_out:
            count_emb = self.count_embs(count_idx)
            cat_arr.append(count_emb.reshape(-1,self.emb_size))
        if self.aud_targs: # Create new h if conditional predictions
            color_emb = self.color_embs(color_idx)
            cat_arr.append(color_emb.reshape(-1,self.emb_size))
            shape_emb = self.shape_embs(shape_idx)
            cat_arr.append(shape_emb.reshape(-1,self.emb_size))
        if len(cat_arr)>1:
            h = torch.cat(cat_arr, axis=-1)
            h = self.aud_projection(h)
        feats = self.cnn(x)
        feats = self.pos_encoder(feats)
        feat = self.extractor(h.unsqueeze(1), feats)
        h = self.rnn(feat.mean(1),h)
        loc = self.locator(h)
        if self.obj_recog and not self.aud_targs:
            color = self.color(h)
            shape = self.shape(h)
        else:
            color,shape = [],[]
        if self.rew_recog:
            rew = self.pavlov(h)
        else:
            rew = []
        self.h = h
        return loc,color,shape,rew

class PooledRNNLocator(RNNLocator):
    def __init__(self,**kwargs):
        super().__init__(**kwargs)
        self.pos_encoder = NullOp()
        self.extractor = Pooler(self.cnn.shapes[-1],
                                emb_size=self.emb_size,
                                ksize=5)
        print("Using PooledRNNLocator")

class ConcatRNNLocator(RNNLocator):
    def __init__(self,**kwargs):
        super().__init__(**kwargs)
        self.pos_encoder = NullOp()
        self.extractor = Concatenater(self.cnn.shapes[-1],
                                      emb_size=self.emb_size,
                                      ksize=5)
        print("Using ConcatRNNLocator")

class CNNBase(nn.Module, CustomModule):
    def __init__(self, img_shape=(3,84,84), act_fxn="ReLU",
                                            emb_size=512,
                                            attn_size=64,
                                            n_heads=6,
                                            feat_bnorm=True,
                                            **kwargs):
        """
        img_shape: tuple of ints (chan, height, width)
            the incoming image size
        act_fxn: str
            the name of the desired activation function
        emb_size: int
            the size of the "embedding" layer which is just the size
            of the final output channel dimension
        attn_size: int
            the size of the attentional features for the multi-head
            attention mechanism
        n_heads: int
            the number of attention heads in the multi-head attention
        bnorm: bool
            
        """
        super().__init__()
        self.img_shape = img_shape
        self.act_fxn = act_fxn
        self.emb_size = emb_size
        self.attn_size = attn_size
        self.n_heads = n_heads
        self.bnorm = feat_bnorm

    def get_conv_block(self, in_chan, out_chan, ksize=3, 
                                          stride=1,
                                          padding=0,
                                          bnorm=True,
                                          act_fxn="ReLU",
                                          drop_p=0):
        """
        returns a set of operations that make up a single layer in
        a convolutional neural network

        in_chan: int
            the number of incoming channels
        out_chan: int
            the number of outgoing channels
        ksize: int
            the size of the convolutional kernel
        stride: int
            the stride length of the convolutional kernel
        padding: int
            the padding of the convolution
        bnorm: bool
            determines if batch normalization should be used
        act_fxn: str
            the name of the activation function
        drop_p: float [0,1]
            the probability of setting an activation to 0
        """
        block = []
        block.append(nn.Conv2d(in_chan,out_chan,ksize,
                                                stride=stride,
                                                padding=padding))
        if bnorm:
            block.append(nn.BatchNorm2d(out_chan))
        if act_fxn is not None:
            block.append(globals()[act_fxn]())
        if drop_p > 0:
            block.append(nn.Dropout(drop_p))
        return block

class SimpleCNN(CNNBase):
    """
    Simple model
    """
    def __init__(self, emb_size, intm_attn=0, **kwargs):
        """
        emb_size: int
        intm_attn: int
            an integer indicating the number of layers for an attention
            layer in between convolutions
        """
        super().__init__(**kwargs)
        self.emb_size = emb_size
        self.intm_attn = intm_attn
        self.conv_blocks = nn.ModuleList([])
        self.intm_attns = nn.ModuleList([])
        self.shapes = []
        shape = self.img_shape[-2:]
        self.shapes.append(shape)
        if self.img_shape[1] <= 84:
            chans = [32,64,128,256,self.emb_size]
            stride = 1
            ksize = 3
        else:
            chans = [3,32,64,128,256,self.emb_size]
            stride = 2
            ksize = 5
            print("using extra layer for larger image size")
        self.chans = chans
        padding = 0
        block = self.get_conv_block(in_chan=self.img_shape[-3],
                                    out_chan=self.chans[0],
                                    ksize=ksize,
                                    stride=stride,
                                    padding=padding,
                                    bnorm=self.bnorm,
                                    act_fxn=self.act_fxn,
                                    drop_p=0)
        self.conv_blocks.append(nn.Sequential(*block))
        shape = update_shape(shape, kernel=ksize, stride=stride,
                                                  padding=padding)
        self.shapes.append(shape)
        if self.intm_attn > 0:
            attn = ConvAttention(chans[0], shape,
                                           n_layers=self.intm_attn,
                                           attn_size=self.attn_size,
                                           act_fxn=self.act_fxn)
            self.itmd_attns.append(attn)
        for i in range(len(chans)-1):
            if i in {1,3}: stride = 2
            else: stride = 1
            block = self.get_conv_block(in_chan=chans[i],
                                        out_chan=chans[i+1],
                                        ksize=ksize,
                                        stride=stride,
                                        padding=padding,
                                        bnorm=self.bnorm,
                                        act_fxn=self.act_fxn,
                                        drop_p=0)
            self.conv_blocks.append(nn.Sequential(*block))
            shape = update_shape(shape, kernel=ksize, stride=stride,
                                                      padding=padding)
            self.shapes.append(shape)
            print("model shape {}: {}".format(i,shape))
            if self.intm_attn > 0:
                attn = ConvAttention(chans[0], shape,
                                               n_layers=self.intm_attn,
                                               attn_size=self.attn_size,
                                               act_fxn=self.act_fxn)
                self.itmd_attns.append(attn)
        self.seq_len = shape[0]*shape[1]

    def forward(self, x, *args, **kwargs):
        """
        x: float tensor (B,C,H,W)
        """
        fx = x
        for i,block in enumerate(self.conv_blocks):
            fx = block(fx)
            if i < len(self.intm_attns):
                fx = self.intm_attns(fx)
        return fx.reshape(fx.shape[0],fx.shape[1],-1).permute(0,2,1)

class MediumCNN(CNNBase):
    """
    Middle complexity model
    """
    def __init__(self, emb_size, intm_attn=0, **kwargs):
        """
        emb_size: int
        intm_attn: int
            an integer indicating the number of layers for an attention
            layer in between convolutions
        """
        super().__init__(**kwargs)
        self.emb_size = emb_size
        self.intm_attn = intm_attn
        self.conv_blocks = nn.ModuleList([])
        self.intm_attns = nn.ModuleList([])
        self.shapes = []
        shape = self.img_shape[-2:]
        self.shapes.append(shape)
        chans = [8,32,64,128,256,self.emb_size]
        stride = 2
        ksize = 7
        self.chans = chans
        padding = 0
        block = self.get_conv_block(in_chan=self.img_shape[-3],
                                    out_chan=self.chans[0],
                                    ksize=ksize,
                                    stride=stride,
                                    padding=padding,
                                    bnorm=self.bnorm,
                                    act_fxn=self.act_fxn,
                                    drop_p=0)
        self.conv_blocks.append(nn.Sequential(*block))
        shape = update_shape(shape, kernel=ksize, stride=stride,
                                                  padding=padding)
        self.shapes.append(shape)
        if self.intm_attn > 0:
            attn = ConvAttention(chans[0], shape,
                                           n_layers=self.intm_attn,
                                           attn_size=self.attn_size,
                                           act_fxn=self.act_fxn)
            self.itmd_attns.append(attn)

        ksize = 3
        for i in range(len(chans)-1):
            if i in {1,3}: stride = 2
            else: stride = 1
            block = self.get_conv_block(in_chan=chans[i],
                                        out_chan=chans[i+1],
                                        ksize=ksize,
                                        stride=stride,
                                        padding=padding,
                                        bnorm=self.bnorm,
                                        act_fxn=self.act_fxn,
                                        drop_p=0)
            self.conv_blocks.append(nn.Sequential(*block))
            shape = update_shape(shape, kernel=ksize, stride=stride,
                                                      padding=padding)
            self.shapes.append(shape)
            print("model shape {}: {}".format(i,shape))
            if self.intm_attn > 0:
                attn = ConvAttention(chans[0], shape,
                                               n_layers=self.intm_attn,
                                               attn_size=self.attn_size,
                                               act_fxn=self.act_fxn)
                self.itmd_attns.append(attn)
        self.seq_len = shape[0]*shape[1]

    def forward(self, x, *args, **kwargs):
        """
        x: float tensor (B,C,H,W)
        """
        fx = x
        for i,block in enumerate(self.conv_blocks):
            fx = block(fx)
            if i < len(self.intm_attns):
                fx = self.intm_attns(fx)
        return fx.reshape(fx.shape[0],fx.shape[1],-1).permute(0,2,1)

class RNNFwdDynamics(LocatorBase):
    """
    This model takes in the current observation and makes a prediction
    of the next state of the game. A separate Decoding model is used
    to constrain these states off of the pixels of the game.
    """
    def __init__(self, deconv_type="SimpleDeconv", rnn_type="GRUCell",
                                                   cnn_type="SimpleCNN",
                                                   aud_targs=False,
                                                   fixed_h=False,
                                                   deconv_emb_size=None,
                                                   **kwargs):
        """
        cnn_type: str
            the class of cnn to use for extracting features from the
            image
        deconv_type: str
            the class of deconv to use for creating features from the
            image
        rnn_type: str
            the class of rnn to use for the temporal model
        aud_targs: bool
            if true, a color and shape must be specified at each step. 
            This creates two separate embeddings that are concatenated
            to the hidden state and projected down into the appropriate
            size for feature extraction and for the rnn. Stands for 
            audible targs
        fixed_h: bool
            if true, the h value is reset at each step in the episode
        """
        super().__init__(**kwargs)
        if deconv_emb_size is not None:
            self.emb_size = deconv_emb_size
            kwargs['emb_size'] = self.emb_size
        self.cnn_type = cnn_type
        self.deconv_type = deconv_type
        self.rnn_type = rnn_type
        self.aud_targs = aud_targs
        self.fixed_h = fixed_h
        if self.fixed_h: print("USING FIXED H VECTOR!!")
        self.deconv = globals()[self.deconv_type](**kwargs)
        self.cnn = globals()[self.cnn_type](**kwargs)
        self.pos_encoder = PositionalEncoder(self.cnn.seq_len,
                                             self.emb_size)
        self.extractor = Attncoder(1, emb_size=self.emb_size,
                                 attn_size=self.attn_size,
                                 n_layers=self.dec_layers,
                                 n_heads=self.n_heads,
                                 act_fxn=self.act_fxn,
                                 use_mask=False,
                                 init_decs=False,
                                 gen_decs=False,
                                 prob_embs=self.prob_embs,
                                 prob_attn=self.prob_attn)
        self.encoder = MuSig(h_size=self.emb_size,
                             feat_size=self.emb_size,
                             s_size=self.emb_size)

        # Learned initialization for rnn hidden vector
        self.h_shape = (1,self.emb_size)
        self.h_init = torch.randn(self.h_shape)
        divisor = float(np.sqrt(self.emb_size))
        self.h_init = nn.Parameter(self.h_init/divisor)

        self.count_embs = nn.Embedding(self.n_numbers, self.emb_size)
        a_size = self.emb_size
        if self.aud_targs:
            self.color_embs = nn.Embedding(self.n_colors, self.emb_size)
            self.shape_embs = nn.Embedding(self.n_shapes, self.emb_size)
            a_size = 3*self.emb_size
        self.rssm = RSSM(h_size=self.emb_size,
                         s_size=self.emb_size,
                         a_size=a_size,
                         rnn_type=self.rnn_type)

    def reset_h(self, batch_size=1):
        """
        returns an h that is of shape (B,E)
        """
        self.h = self.h_init.repeat(batch_size,1)
        return self.h

    def forward(self, x, h=None, color_idx=None,
                                 shape_idx=None,
                                 count_idx=None,
                                 mu=None,
                                 sigma=None):
        """
        x: torch float tensor (B,C,H,W)
            must be None if mu and sigma are not None
        h: optional float tensor (B,E)
        color_idx: long tensor (B,1)
        shape_idx: long tensor (B,1)
        count_idx: long tensor (B,1)
        mu: float tensor (B,E), optional
            if this is not none, x must be none and sigma must be not
            none
        sigma: float tensor (B,E), optional
            if this is not none, x must be none and mu must be not
            none
        """
        assert count_idx is not None, "Must have count index"
        assert x is None or (mu is None and sigma is None)
        if h is None:
            h = self.h
        if self.fixed_h:
            h = self.reset_h(len(x))

        if x is not None:
            feats = self.cnn(x)
            feats = self.pos_encoder(feats)
            feat = self.extractor(h.unsqueeze(1), feats)
            feat = feat.mean(1)
            mu, sigma = self.encoder(h,feat)

        count_emb = self.count_embs(count_idx)
        emb = count_emb.reshape(-1,self.emb_size)
        if self.aud_targs:
            cat_arr = [emb]
            color_emb = self.color_embs(color_idx)
            cat_arr.append(color_emb.reshape(-1,self.emb_size))
            shape_emb = self.shape_embs(shape_idx)
            cat_arr.append(shape_emb.reshape(-1,self.emb_size))
            emb = torch.cat(cat_arr, axis=-1)
        s = sample_s(mu, sigma)
        h,pred_mu,pred_sigma = self.rssm(h,s,emb)
        self.h = h
        return h,mu,sigma,pred_mu,pred_sigma

    def decode(self, s):
        """
        s: float tensor (B,E)
        """
        return self.deconv(s) # (B,C,H,W)

class MuSig(nn.Module, CustomModule):
    """
    A simple class to assist in creating state vectors for the rssm
    """
    def __init__(self, h_size, feat_size, s_size, min_sigma=0.0001):
        """
        h_size: int
            size of h_vector
        feat_size: int
            size of feature vector
        s_size: int
            size of the state vector
        """
        super().__init__()
        self.h_size = h_size
        self.min_sigma = min_sigma
        self.feat_size = feat_size
        self.projection=nn.Linear(self.h_size+self.feat_size,2*s_size)

    def forward(self, h, feat):
        """
        h: torch float tensor (B,H)
        feat: torch float tensor (B,F)
        """
        inpt = torch.cat([h,feat],dim=-1)
        musigma = self.projection(inpt)
        mu, sigma = torch.chunk(musigma, 2, dim=-1)
        sigma = F.softplus(sigma) + self.min_sigma
        return mu, sigma

def sample_s(mu,sigma):
    """
    A simple helper function to sample a gaussian

    mu: float tensor (..., N)
        the means of the gaussian
    sigma: float tensor (..., N)
        the standard deviations of the gaussian
    """
    return mu + sigma*torch.randn_like(sigma)

class PooledRNNFwdDynamics(RNNFwdDynamics):
    def __init__(self,**kwargs):
        super().__init__(**kwargs)
        self.pos_encoder = NullOp()
        self.extractor = Pooler(self.cnn.shapes[-1],
                                emb_size=self.emb_size,
                                ksize=5)
        print("Using PooledRNNFwdDynamics")

class ConcatRNNFwdDynamics(RNNFwdDynamics):
    def __init__(self,**kwargs):
        super().__init__(**kwargs)
        self.pos_encoder = NullOp()
        self.extractor = Concatenater(self.cnn.shapes[-1],
                                      emb_size=self.emb_size,
                                      ksize=5)
        print("Using ConcatRNNFwdDynamics")

def deconv_block(in_depth, out_depth, ksize=3, stride=1,
                                               padding=0,
                                               bnorm=False,
                                               act_fxn='ReLU',
                                               drop_p=0):
    """
    Creates a deconvolution layer

    in_depth: int
    out_depth: int
    ksize: int
    stride: int
    padding: int
    bnorm: bool
        determines if a batchnorm layer should be inserted just after
        the deconvolution
    act_fxn: str
        the name of the activation class
    drop_p: float
        the probability of an activation being dropped
    """
    block = []
    block.append(nn.ConvTranspose2d(in_depth, out_depth,
                                              ksize,
                                              stride=stride,
                                              padding=padding))
    if bnorm:
        block.append(nn.BatchNorm2d(out_depth))
    block.append(nn.Dropout(drop_p))
    if act_fxn is not None:
        block.append(getattr(nn, act_fxn)())
    return nn.Sequential(*block)


class SimpleDeconv(nn.Module):
    """
    This model is used to make observation predictions. It takes in
    a single state vector and transforms it to an image (C,H,W)
    """
    def __init__(self, emb_size, img_shape, deconv_start_shape=(512,7,7),
                                            deconv_ksizes=None,
                                            deconv_strides=None,
                                            deconv_lnorm=True,
                                            fwd_bnorm=False,
                                            drop_p=0,
                                            end_sigmoid=False,
                                            **kwargs):
        """
        deconv_start_shape - list like [channel1, height1, width1]
            the initial shape to reshape the embedding inputs
        deconv_ksizes - list like of ints
            the kernel size for each layer
        deconv_stides - list like of ints
            the strides for each layer
        img_shape - list like [channel2, height2, width2]
            the final shape of the decoded tensor
        emb_size - int
            size of belief vector h
        deconv_lnorm: bool
            determines if layer norms will be used at each layer
        fwd_bnorm: bool
            determines if batchnorm will be used
        drop_p - float
            dropout probability at each layer
        end_sigmoid: bool
            if true, the final activation is a sigmoid. Otherwise
            there is no final activation
        """
        super().__init__()
        self.start_shape = deconv_start_shape
        self.img_shape = img_shape
        self.emb_size = emb_size
        self.drop_p = drop_p
        self.bnorm = fwd_bnorm
        self.end_sigmoid = end_sigmoid
        self.strides = deconv_strides
        self.ksizes = deconv_ksizes
        self.lnorm = deconv_lnorm
        print("deconv using bnorm:", self.bnorm)

        if self.ksizes is None:
            self.ksizes = [9,5,5,4,4,4,4,4,4,4]
        if self.strides is None:
            if self.start_shape[-1] == 7:
                self.strides = [2,1,1,1,2,2,2,1,1]
            else:
                self.strides = [2,1,1,1,2,2,1,1,1]

        flat_start = int(np.prod(deconv_start_shape))
        modules = []
        if self.lnorm:
            modules.append(nn.LayerNorm(emb_size))
        modules.append(nn.Linear(emb_size, flat_start))
        if self.bnorm:
            modules.append(nn.BatchNorm1d(flat_start))
        modules.append(Reshape((-1, *deconv_start_shape)))

        depth, height, width = deconv_start_shape
        first_ksize = self.ksizes[0]
        first_stride = self.strides[0]
        self.sizes = []
        deconv = deconv_block(depth, depth, ksize=first_ksize,
                                            stride=first_stride,
                                            padding=0,
                                            bnorm=self.bnorm,
                                            drop_p=self.drop_p)
        height, width = update_shape((height,width),kernel=first_ksize,
                                                    stride=first_stride,
                                                    op="deconv")
        print("Img shape:", self.img_shape)
        print("Start Shape:", deconv_start_shape)
        print("h:", height, "| w:", width)
        self.sizes.append((height, width))
        modules.append(deconv)

        padding = 0
        i = 0
        while height < self.img_shape[-2] and width < self.img_shape[-1]:
            i+=1
            ksize =  self.ksizes[i]
            stride = self.strides[i]
            if self.lnorm:
                modules.append(nn.LayerNorm((depth,height,width)))
            height, width = update_shape((height,width), kernel=ksize,
                                                         stride=stride,
                                                         padding=padding,
                                                         op="deconv")
            if height==self.img_shape[-2] and width==self.img_shape[-1]:
                end_depth = self.img_shape[-3]
            else:
                end_depth = max(depth // 2, 16)
            modules.append(deconv_block(depth, end_depth,
                                        ksize=ksize, padding=padding,
                                        stride=stride, bnorm=self.bnorm,
                                        drop_p=drop_p))
            depth = end_depth
            self.sizes.append((height, width))
            print("h:", height, "| w:", width, "| d:", depth)
        
        # TODO: implement CutOut method
        diff = height-self.img_shape[-2]
        if diff > 0:
            k = diff + 1
            modules.append(nn.Conv2d(depth, self.img_shape[0], k))
            height, width = update_shape((height,width), kernel=k)
            self.sizes.append((height, width))
        if self.end_sigmoid: 
            modules.append(nn.Sigmoid())
        print("decoder:", height, width)
        self.sequential = nn.Sequential(*modules)

    def forward(self, x):
        """
        x - torch FloatTensor (B,E)
        """
        return self.sequential(x)

    def extra_repr(self):
        s = "start_shape={}, img_shape={}, bnorm={}, drop_p={}"
        return s.format(self.start_shape, self.img_shape, self.bnorm,
                                                        self.drop_p)

class Pooler(nn.Module):
    """
    A simple class to act as a dummy extractor that actually performs
    a final convolution followed by a global average pooling
    """
    def __init__(self, shape, emb_size=512, ksize=5):
        """
        shape: tuple of ints (H,W)
        emb_size: int
        ksize: int
        """
        super().__init__()
        self.emb_size = emb_size
        self.ksize = ksize
        self.shape = shape
        self.conv = nn.Conv2d(self.emb_size, self.emb_size, self.ksize)
        self.activ = nn.ReLU()
        self.layer = nn.Sequential( self.conv, self.activ)

    def forward(self, h, x):
        """
        h: dummy
        x: torch FloatTensor (B,S,E)
            the features from the cnn
        """
        shape = (len(x), self.emb_size, self.shape[0], self.shape[1])
        x = x.permute(0,2,1).reshape(shape)
        fx = self.layer(x)
        return fx.reshape(*shape[:2],-1).mean(-1).unsqueeze(1) # (B,1,E)

class Concatenater(nn.Module):
    """
    A simple class to act as a dummy extractor that actually performs
    a another convolution followed by a feature concatenation and 
    nonlinear projection to a single feature vector.
    """
    def __init__(self, shape, emb_size=512, ksize=5, h_size=1000):
        """
        shape: tuple of ints (H,W)
        emb_size: int
        ksize: int
        """
        super().__init__()
        self.emb_size = emb_size
        self.ksize = ksize
        self.shape = shape
        self.h_size = h_size
        self.conv = nn.Conv2d(self.emb_size, self.emb_size, self.ksize)
        self.activ = nn.ReLU()
        self.layer = nn.Sequential( self.conv, self.activ)
        new_shape = update_shape(self.shape, kernel=ksize,
                                             stride=1,
                                             padding=0)
        flat_size = new_shape[-2]*new_shape[-1]*self.emb_size
        self.collapser = nn.Sequential(
                    nn.Linear(flat_size,self.h_size),
                    nn.ReLU(),
                    nn.Linear(self.h_size, self.emb_size)
                    )


    def forward(self, h, x):
        """
        h: dummy
        x: torch FloatTensor (B,S,E)
            the features from the cnn
        """
        shape = (len(x), self.emb_size, self.shape[0], self.shape[1])
        x = x.permute(0,2,1).reshape(shape)
        fx = self.layer(x).reshape(len(x), -1)
        fx = self.collapser(fx)
        return fx.reshape(len(x),1,self.emb_size) # (B,1,E)

class NullOp(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, x):
        return x

class Reshape(nn.Module):
    """
    Reshapes the activations to be of shape (B, *shape) where B
    is the batch size.
    """
    def __init__(self, shape):
        super(Reshape, self).__init__()
        self.shape = shape

    def forward(self, x):
        return x.view(self.shape)

    def extra_repr(self):
        return "shape={}".format(self.shape)

class Cutout(nn.Module):
    """
    Takes the center of a pixel grid with the specified size
    """
    def __init__(self, cut_height, cut_width=None):
        """
        cut_height: int
            the height of the cutout
        cut_width: int or None
            the width of the cutout. if None, defaults to height
        dims: tuple of ints
            the dimension of the height and width respectively
        """
        self.cut_height = cut_height
        self.cut_width = cut_width
        if cut_width is None:
            self.cut_width = self.cut_height

    def forward(self, x):
        """
        x: torch Tensor (...,H,W)
            must have the height and width as the final dimensions
        """
        if x.shape[-2]>self.cut_height:
            half_diff = (x.shape[-2]-self.cut_height)//2
            x = x[...,half_diff:half_diff+self.cut_height,:]
        if x.shape[-1]>self.cut_width:
            half_diff = (x.shape[-1]-self.cut_width)//2
            x = x[...,half_diff:half_diff+self.cut_width]
        return x
