# Colab users, uncomment the following block to help clear out notebook state when re-running the cell.
"""
# don't forget these too:
# !pip3 install tiktoken
# If you don't have torch 2.0 on whatever environment you're using:
# !pip3 install --upgrade torch
try:
  _ = get_ipython().__class__.__name__
  ## we set -f below to avoid prompting the user before clearing the notebook state
  %reset -f
except NameError:
  pass ## we're still good
"""

import itertools
import argparse
from typing import Any, Literal
from functools import partial
import subprocess
import random

import zipfile
import math
import os

import einops
import rich
import torch
import torch.nn.functional as F
from torch import nn
import polars as pl
import wandb

# This seems like one of the best choices right now for a fast/lightweight/simple tokenizer.
import tiktoken


print = rich.print


################
# Introduction #
################

# This code was built from the ground up to support extremely rapid experimentation for solo researchers and small teams. It's meant to
# be hackable nearly anywhere with minimal effort/side effects, which is why you might see more of a flat layout. It's also quite fast.
#
# The codebase is specifically designed for single A100s for now, but may expand with more GPU support in the future, depending. I originally
# used Karpathy's nanoGPT as well as some of my other work as a reference when writing this, though this codebase is very much
# its own thing at this point.
#
# If you found this codebase useful or informative, please consider supporting me directly at https://www.patreon.com/tysam . If you'd like
# to speak about a contract or a consulting opportunity, feel free to reach out at hi [dot] re [dot] tysam [atsymbol] gmail [dot] com.
# I'd love to hear from you!
#
# Now, on with the code!


##############################
#      Hyperparameters       #
##############################

# Note: The automatic rescaling of hyperparameters based on batchsize/etc is currently a work in progress.
# This code assumes 40 GB-limit A100s for the scale-based hyperparameters, you may have to do some tinkering if you have a different setup.
# So far, most of the tested configs have been between ~46 M and 1.5B or so, and have done moderately well.

# This parameter determines the final size of the model. Roughly, num_model_params ~= model_scale * 49 M (# of params in the base model), but it scales nonlinearly. (#TODO is to make this more straight in the future)
# Model scales other than 1.0 are in alpha currently -- they should run okay, but are almost certainly not tuned efficiently yet! This should hopefully be addressed in a future update.
model_scale         = 1.0    # OOM-tested from ~.5ish (28 M) to 148 (~3 B). Sets the model size. One of the most important hyperparameters. Supports noninteger values (2.3, etc)
max_sequence_length = 1024   # Can go up or down. Mostly tested up to 1024, some models can avoid OOMs even with length 8192 (not really tested)
gpu_token_capacity  = 114688 # This is an amount that doesn't OOM on A100 at model_scale 1, length 1024. May need to change if you have a different GPU. Note: Hyperparameter tunings are currently based on the 40 GB limit of the A100.

# Approximates the amount of tokens the GPU can hold based upon the scale of the model (scaled somewhat conservatively to avoid most OOMs. May OOM in some weird edgecases.)
# Batchsize is determined automatically based upon the current sequence length and the rough token-capacity of the GPU for a given model.
tokens_per_batch_capacity  = math.floor(gpu_token_capacity / (1.52174 + .482 * model_scale**(.87)))

# We support fractional model factors, this picks dimensions that the A100 can efficiently use.
to_nearest_64 = lambda x: round(x/64) * 64


# The default model here below is roughly ~46M parameters or so.
hyp = {
    'opt': {
        'lr_mult': {
            'base': 2.62, # The base_lr itself is derived from a scaling equation fit to GPT-3 parameters. This multiplier impacts all parameters, including those in the default group
            'position_bias': 100.,
            'non_dot_products': 32.,
            'output_layer': 2.,
        },
        'weight_decay': 2.**4,     # This is the weight decay when the loss = 0., we approach it exponentially. Somewhat slows overfitting.
        'total_train_steps': 1000, # We can run effectively infinitely, but is 1000 by default for the inference demo. For infinite runs, you can use the saved checkpoints from disk.
        'microbatch': {            # The microbatch scheduler assumes a power law decay schedule for the grad norm, and adjusts the microbatch size (minimum 1) to enforce it.
            'sample_every': 5,     # Sampling grad norm can be a bit expensive, so we do it every n steps instead.
            'scale_lr': 1e-1,      # Microbatch update rate
        },
        'eval_every': 50,          # how many train iterations per eval round (we don't include eval time in our performance stats). Good to set to 10-20 for larger (~800M+ networks)
        'save_every_n_evals': 2,   # Good to set this low for larger networks
        'num_eval_tokens': 153600, # Total # tokens total to eval over, divided into max_sequence_length-long sequences
        'warmup_steps': 100,       # For training stability in the main body of the network. (#TODO: Investigate the warmup imact a bit more)
    },
    'net': {
        'residual_depth': to_nearest_64(384 * math.log2(1.+model_scale)),
        'qk_dim_div': 8,
        'expand_factor': 2,
        'num_blocks': round(8 * math.log2(1.+model_scale)),
    },
    'misc': {
        'num_tokens': 50304, # Rounded to the nearest value of 64 for efficiency
        'num_special_tokens': 4,
        'causal_token': 50304,
        'planning_token': 50305,
        'acting_token': 50306,
        'mask_token': 50307,
        'sequence_length': {
            'max': max_sequence_length,
            'initial': 32,      # Very short initial sequence length seems to help a lot
            'growth_steps': 80, # We double the sequence length during training every n steps up to the maximum
        },
        'device': 'cuda',
        'dtype': torch.bfloat16,
        'data_location': 'data.pt',
    }
}


def change_gpu_token_capacity(factor: float):
    global gpu_token_capacity
    gpu_token_capacity = int(factor * 114688)


def change_model_scale(
        scale: float, depth: int | None = None, 
        width: int | None = None, 
        num_heads: int = 1,
) -> tuple[int, int, int, int]:
    global model_scale, tokens_per_batch_capacity, hyp, gpu_token_capacity
    if depth is not None or width is not None:
        assert width is not None and depth is not None
        width = to_nearest_64(width)
        depth = depth
    else:
        width = to_nearest_64(384 * math.log2(1.+scale))
        depth = round(8 * math.log2(1.+scale))

    hyp['net']['residual_depth'] = width
    hyp['net']['num_blocks'] = depth


    # Measure number of parameters
    net = make_net(dict(depth=depth, width=width, linear_value=False, num_heads=num_heads))
    num_params = sum(p.numel() for p in net.parameters() if p.requires_grad)
    num_non_embedding_params = sum(p.numel() for m in (net.net_dict['attn_layers'] + [net.net_dict['norm']]) for p in m.parameters())
    del net

    # Set actual model scale
    default_params = 46_009_736
    model_scale = num_params / default_params

    # Needed for computation to work
    tokens_per_batch_capacity  = math.floor(gpu_token_capacity / (1.52174 + .482 * model_scale**(.87)))

    return num_params, num_non_embedding_params, depth, width



#############################################
#                Dataloader                 #
#############################################

if not os.path.exists(hyp['misc']['data_location']):
    print("downloading data and tokenizing (1-2 min)")

    raw_data_source = 'https://wikitext.smerity.com/wikitext-103-raw-v1.zip'
    raw_data_cache = './data_raw/' # where to cache the data after downloading

    if not os.path.isfile(raw_data_cache):
        os.makedirs(raw_data_cache, exist_ok=True)

        # Needed due to the website 403-blocking python agents for download, it seems? Many thanks to Smerity for re-hosting these after the main files went down. <3 :')
        subprocess.run(["wget", raw_data_source, "-O", raw_data_cache+"data.zip"], stdout=subprocess.PIPE)

    with zipfile.ZipFile('data_raw/data.zip', 'r') as zip_ref:
        zip_ref.extractall('data_raw/')

    with open('data_raw/wikitext-103-raw/wiki.train.raw') as data_file:
        raw_train_data = data_file.read()

    with open('data_raw/wikitext-103-raw/wiki.valid.raw') as data_file:
        raw_eval_data = data_file.read()


    tokenizer = tiktoken.get_encoding("gpt2")
    raw_tokenized_train = tokenizer.encode_ordinary(raw_train_data)
    raw_tokenized_eval  = tokenizer.encode_ordinary(raw_eval_data)

    train_tokenized = torch.tensor(raw_tokenized_train, device=hyp['misc']['device'], dtype=torch.int) # int64 is likely overkill for the amount of tokens we have...
    eval_tokenized  = torch.tensor(raw_tokenized_eval,  device=hyp['misc']['device'], dtype=torch.int)

    data = {
        'train': train_tokenized,
        'eval': eval_tokenized
        }

    torch.save(data, hyp['misc']['data_location'])
    print("completed the tokenization process!")

else:
    ## This is effectively instantaneous, and takes us practically straight to where the dataloader-loaded dataset would be. :)
    ## So as long as you run the above loading process once, and keep the file on the disc it's specified by default in the above
    ## hyp dictionary, then we should be good. :)
    data = torch.load(hyp['misc']['data_location'])


########################################
#              Constants               #
########################################

with torch.no_grad():
    # Create the base arrays for the learnable linear positional bias. This helps save some memory consumption & processing time
    bias_range                    = torch.arange(-hyp['misc']['sequence_length']['max']+1, 1).to(hyp['misc']['device'], torch.bfloat16)
    position_bias_base            = bias_range.unsqueeze(0) - bias_range.unsqueeze(1)
    negative_infinity_matrix_base = torch.empty_like(position_bias_base).fill_(-float("inf"))
    causal_mask = torch.tril(torch.ones((hyp['misc']['sequence_length']['max'], hyp['misc']['sequence_length']['max']), device=hyp['misc']['device'], dtype=torch.bool))


# Used in the dataloader to select indexes in a sequence. Preallocated for slight efficiency.
batch_index_offsets = torch.arange(0, hyp['misc']['sequence_length']['max']+1, dtype=torch.long, device=hyp['misc']['device'])


#############################################
#            Network Components             #
#############################################

class LatentAttentionBlock(nn.Module):
    """ Efficient fused latent-space attention block. Linear keys and queries, nonlinear values."""
    def __init__(self, num_dim, linear_value: bool, num_heads: int):
        super().__init__()
        # Layer dim parameters. Play around with these, there's likely some undiscovered stuff still!
        self.dim        = num_dim
        self.qk_dim     = self.dim//hyp['net']['qk_dim_div']
        self.v_dim      = num_dim
        self.expand_dim = num_dim * hyp['net']['expand_factor']
        self.linear_value = linear_value 
        self.num_heads = num_heads

        # Main layer weights
        self.norm    = nn.LayerNorm(self.dim, bias=False)
        self.expand  = nn.Parameter(.5 * 1./hyp['net']['residual_depth']**.5 * 1./hyp['net']['expand_factor']                               * torch.randn(2*self.qk_dim+2*self.expand_dim, self.dim))
        self.project = nn.Parameter(1. * 1./hyp['net']['residual_depth']**.5 * 1./hyp['net']['expand_factor'] * 1./hyp['net']['num_blocks'] * torch.randn((self.dim, self.expand_dim)))

        # Learnable linear positional encodings. Similar to but different than https://arxiv.org/abs/2108.12409
        # Has a high lr mult applied to it so that each layer can learn its own attention scale.
        self.position_bias_mult = nn.Parameter(torch.tensor(1., device='cuda'))

    def make_mask(
            self, 
            x: torch.Tensor, 
            first_acting_token_idx: int | None = None,
            last_acting_token_idx: int | None = None,
    ):
        seq_len = x.shape[1]
        attn_mask = torch.where(
            causal_mask[:seq_len, :seq_len], 
            F.softplus(self.position_bias_mult) * position_bias_base[:seq_len, :seq_len], 
            negative_infinity_matrix_base[:seq_len, :seq_len]
        )
        if first_acting_token_idx is not None:
            assert last_acting_token_idx is not None
            assert last_acting_token_idx > first_acting_token_idx
            first_acting_token_idx = None if first_acting_token_idx >= seq_len else first_acting_token_idx
            last_acting_token_idx = None if last_acting_token_idx >= seq_len else last_acting_token_idx
            attn_mask[first_acting_token_idx:, last_acting_token_idx:] = (
                F.softplus(self.position_bias_mult) 
                * position_bias_base[first_acting_token_idx:seq_len, last_acting_token_idx:seq_len]
            )
        return attn_mask

    def forward(
            self, 
            x: torch.Tensor, 
            first_acting_token_idx: int | None = None,
            last_acting_token_idx: int | None = None,
    ):
        residual = x

        attn_mask = self.make_mask(x, first_acting_token_idx, last_acting_token_idx)
        # Shared LayerNorm for linear layers and attention
        x = self.norm(x)

        # Fused into one kernel for memory+speed/etc
        query, key, linear, pre_gelu = F.linear(x, self.expand).split((self.qk_dim, self.qk_dim, self.expand_dim, self.expand_dim), dim=-1)

        # Compute GeGLU (one portion of the channels this will stay locally, another will become the nonlinear value for attention)
        geglu = linear * F.gelu(pre_gelu)

        # Partition between the input values and the v dim values
        if self.linear_value:
            geglu_local, _ = geglu.split((self.expand_dim-self.v_dim, self.v_dim), -1)
            _, geglu_attention_value = pre_gelu.split((self.expand_dim-self.v_dim, self.v_dim), -1)
        else:
            geglu_local, geglu_attention_value = geglu.split((self.expand_dim-self.v_dim, self.v_dim), -1)

        if self.num_heads > 1:
            query, key, geglu_local, geglu_attention_value = map(lambda x: einops.rearrange(x, 'b n (h d) -> b h n d', h=self.num_heads), (query, key, geglu_local, geglu_attention_value))


        # Compute attention. Something to note is that there are no attention heads here. This seemed to work a bit better, maybe due to not needing memory `.contiguous()` calls or similar
        attention = F.scaled_dot_product_attention(query, key, geglu_attention_value, attn_mask=attn_mask)

        if self.num_heads > 1:
            attention = einops.rearrange(attention, 'b h n d -> b n (h d)')
            geglu_local = einops.rearrange(geglu_local, 'b h n d -> b n (h d)')

        # Output linear layer
        out = F.linear(torch.cat([geglu_local, attention], dim=-1), self.project)

        # Add to residual
        x = residual + out

        return x


#############################################
#            Network Definition             #
#############################################

# This may seem like an odd way to define a network, but it's a bit easier to hack into/make quick changes than other methods
class SpeedyLangNet(nn.Module):
    def __init__(self, network_dict):
        super().__init__()
        self.net_dict = network_dict

    def embed(self, x: torch.Tensor) -> torch.Tensor:
        return self.net_dict['embedding'](x)

    def forward(
            self, 
            x: torch.Tensor,
            first_acting_token_idx: int | None = None,
            last_acting_token_idx: int | None = None,
    ):
        if x.dtype == torch.int64:
            x = self.embed(x)
        for attn_block in self.net_dict['attn_layers']:
            x = attn_block(x, first_acting_token_idx, last_acting_token_idx)
        x = self.net_dict['norm'](x)
        x = self.net_dict['outputs'](x)
        return x
    

def make_attn(settings: dict[str, Any]):
    # You can parametrically change anything you want about the attn blocks here
    return LatentAttentionBlock(settings['width'], settings['linear_value'], settings['num_heads'])


def make_net(settings: dict[str, Any]):
    total_num_tokens = hyp['misc']['num_tokens']+hyp['misc']['num_special_tokens']
    network_dict = nn.ModuleDict({
        'embedding': nn.Embedding(total_num_tokens, settings['width'], scale_grad_by_freq=True),
        'attn_layers': nn.ModuleList([make_attn(settings) for _ in range(settings['depth'])]),
        'norm': nn.LayerNorm(settings['width'], bias=False),
        'outputs': nn.Linear(settings['width'], total_num_tokens, bias=False),
    })
    net = SpeedyLangNet(network_dict)
    net = net.to(hyp['misc']['device'], torch.bfloat16)
    net.train()

    # Initialize the embedding and output matrixes, with weights scaled based upon the dimensionality of the network.
    torch.nn.init.normal_(net.net_dict['embedding'].weight.data, std=.25*1./settings['width']**.5)
    torch.nn.init.normal_(net.net_dict['outputs']  .weight.data, std=.5 *1./settings['width']**.5)

    return net


########################################
#          Training Helpers            #
########################################

# Get a single batch item. Currently used in the training loop
@torch.no_grad()
def get_batch(data_dict, key, batchsize, length):
    start_indexes     = torch.randint(len(data_dict[key])-length-1, (batchsize,), device=hyp['misc']['device']) # warning, completely random sampling, not a random derangement, that might help performance a bit!
    sequence_indexes  = start_indexes.unsqueeze(-1) + batch_index_offsets[:length].unsqueeze(0) # slice, as batch_index_offsets are pre-allocated to max length for efficiency
    sampled_sequences = torch.take_along_dim(data_dict[key], sequence_indexes.flatten(), dim=0).view(batchsize, length).long() # have to flatten and reshape due to take_along_dim being 1d

    return sampled_sequences


@torch.no_grad()
def get_causal_data(sequence: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
    targets  = torch.empty_like(sequence).copy_(sequence)

    # Inputs: add special token to beginning
    # Just roll the tensor and replace the first (previously final) token to get the causality going
    inputs = sequence.roll(1, dims=-1)
    inputs[:, 0] = hyp['misc']['causal_token']

    return inputs, targets


@torch.no_grad()
def get_planning_data(
        sequence: torch.Tensor,
        first_acting_token_idx: int,
) -> tuple[torch.Tensor, torch.Tensor]:
    targets = torch.zeros_like(
        sequence, 
        device=hyp['misc']['device'], 
        dtype=torch.long,
    ).copy_(sequence)  # copy sequence to not have negative downstream effects

    inputs = sequence.roll(1, dims=-1)
    inputs [:, first_acting_token_idx:] = hyp['misc']['mask_token']
    inputs[:, 0] = hyp['misc']['planning_token']

    return inputs, targets


@torch.no_grad()
def get_acting_data(
        net: SpeedyLangNet,
        sequence: torch.Tensor,
        planning_output: torch.Tensor,
        first_acting_token_idx: int,
        last_acting_token_idx: int,
        top_k: int = 5,
) -> tuple[torch.Tensor, torch.Tensor]:
    targets = torch.zeros_like(
        sequence, 
        device=hyp['misc']['device'], 
        dtype=torch.long,
    ).copy_(sequence)

    inputs = sequence.roll(1, dims=-1)
    inputs[:, first_acting_token_idx:last_acting_token_idx] = hyp['misc']['acting_token']
    inputs[:, 0] = hyp['misc']['acting_token']
    inputs = net.embed(inputs)
    inputs[:, last_acting_token_idx:] = recombine_outputs(net, planning_output[:, last_acting_token_idx:], top_k)

    return inputs, targets


@torch.no_grad()
def recombine_outputs(net: SpeedyLangNet, planning_output: torch.Tensor, top_k: int) -> torch.Tensor:
    planning_output.grad = None
    values, indices = torch.topk(planning_output, k=top_k, dim=-1)
    normalized_values = values / values.sum(dim=-1, keepdim=True)
    embedded = net.embed(indices)
    weighted = embedded * normalized_values.unsqueeze(-1)
    result = weighted.sum(dim=-2)
    return result


def randomize_masking_rate(mean: float, concentration: int = 8) -> float:
    alpha = mean * concentration
    beta = (1 - mean) * concentration
    return torch.distributions.Beta(alpha, beta).sample()


def get_first_and_last_acting_token_idx(seq_len: int, planning_rate: float, acting_rate: float):
    planning_width = max(2, math.floor(planning_rate * seq_len))
    acting_width = max(1, math.floor(acting_rate * seq_len))

    first_acting_token_idx = seq_len - planning_width
    last_acting_token_idx = first_acting_token_idx + acting_width

    return first_acting_token_idx, last_acting_token_idx


# Make loss function
loss_fn = nn.CrossEntropyLoss(reduction='mean', ignore_index=-1)


##############################
#        Scheduling          #
##############################

# Infinite power law dicay is a simple power law learning rate schedule. seems to perform really well in practice as is simpler than OneCycle to tune.
# Does a linear warmup from a min_initial lr to the max_lr at the peak_step, then decays infinitely with a 1/x**(power_value)-type shape to it.
# These schedulers are multiplicative, that is why they scales from some base value to 1, which is what PyTorch's LambdaLR expects
infinite_power_law_decay    = lambda step, min_initial_mult, peak_step, exponent: min_initial_mult + step/peak_step * (1 - min_initial_mult) if step < peak_step else (step + 1. - peak_step) ** exponent
exp_decay_lr_scheduler_base = lambda step, decay: decay ** step

infinite_powah         = partial(infinite_power_law_decay, min_initial_mult=2e-2, peak_step=hyp['opt']['warmup_steps'], exponent=-.08)
infinite_powah_outputs = partial(infinite_power_law_decay, min_initial_mult=1.,   peak_step=0.,                         exponent=-.2)
pos_bias_decay_lr      = partial(exp_decay_lr_scheduler_base, decay=.995)


def init_param_groups_dict(net, base_lr):
    # the 'scheduler' attribute that we create here is not used by the optimizer, here we just use it to conveniently store all of these attributes.
    param_groups = {}

    # Multiply by our delta over the base lr-scaling curve
    scaled_lr = base_lr * hyp['opt']['lr_mult']['base']

    print("scaled lr:          ", "{:0.8f}".format(scaled_lr))

    # Decay is the default dictionary if there is no parameter name match
    param_groups['decay']                     = {'params': [], 'lr': scaled_lr,                                           'eps': 1e-9, 'betas': (.9,  .95), 'weight_decay': hyp['opt']['weight_decay'],  'scheduler': infinite_powah        }
    param_groups['position_bias_mult']        = {'params': [], 'lr': hyp['opt']['lr_mult']['position_bias']   *scaled_lr, 'eps': 1e-9, 'betas': (.9,  .95), 'weight_decay': 0,                           'scheduler': pos_bias_decay_lr     }
    param_groups['norm', 'bias', 'embedding'] = {'params': [], 'lr': hyp['opt']['lr_mult']['non_dot_products']*scaled_lr, 'eps': 1e-9, 'betas': (.9,  .95), 'weight_decay': 0,                           'scheduler': infinite_powah        }
    param_groups['output']                    = {'params': [], 'lr': hyp['opt']['lr_mult']['output_layer']    *scaled_lr, 'eps': 1e-9, 'betas': (.6,  .95), 'weight_decay': 0,                           'scheduler': infinite_powah_outputs}

    # Helper functions for matching parameters to dictionary keys
    in_list  = lambda name, keyword_list: any(keyword in name for keyword in keyword_list)
    to_tuple = lambda x: x if type(x) == tuple else (x,)

    # In order, search through the dictionary keys, and add to that dictionary if a value in the dictionary key matches the name.
    # 'decay' is the name of the default group, and is the only group with weight decay.
    for name, p in net.named_parameters():
        if p.requires_grad:
            target_param_dict = next(iter([k for k in param_groups.keys() if in_list(name, to_tuple(k))]), 'decay')
            param_groups[target_param_dict]['params'].append(p)

    return param_groups


def get_grad_norm(net):
    # Gets the entire grad norm of the network.
    grad_norm = torch.tensor(0., device=hyp['misc']['device'], dtype=torch.float64)
    for p in net.parameters():
        if p.grad is not None:
            param_norm = p.grad.detach().data.norm(2)
            grad_norm += param_norm.square()
    grad_norm = (grad_norm ** 0.5).item()
    return grad_norm


def grow_sequence_length(old_length, old_batchsize):
    # Dynamically grows the sequence length and changes the batchsize to avoid OOMs
    new_length        = min(2*old_length, hyp['misc']['sequence_length']['max'])
    new_batchsize     = tokens_per_batch_capacity // new_length

    print(f"| increasing sequence length (old: {old_length}, new: {new_length}), adjusting batchsize as necessary to fit (old: {old_batchsize}, new: {new_batchsize})")

    return new_length, new_batchsize


##############################
#          Logging           #
##############################

variables_to_log = ['epoch', 'curr_step', 'train_loss', 'val_loss_causal', 'val_loss_planning', 'val_loss_acting']
# define the printing function and print the column heads
def print_training_details(columns_list, separator_left='  ', separator_right='  |', column_labels_only=False, is_final_entry=False):
    output_line = "|" # start with the left bar

    # Build the print string for the output:
    for column_entry in columns_list:
        output_line += separator_left + column_entry + separator_right

    if column_labels_only:
        print('-'*(len(output_line))) # print an initial upper dividing bar

    print(output_line)

    if column_labels_only or is_final_entry:
        print('-'*(len(output_line))) # print a lower divider bar


# The previous function was a shorter but slightly more heinous lambda, however, this may still cause you some pain. <3 :'(
def format_for_table(var_list, locals):
    int_format     = lambda x: f"{locals[x]}".rjust(len(x))
    default_format = lambda x: f"{locals[x]:0.4f}".rjust(len(x)) if len(f"{locals[x]:0.4f}") < 8 else f"{locals[x]:.4f}"[:8].rjust(len(x))
    blank_format   = lambda x: " "*len(x)

    out_list = [blank_format(v) if v not in locals else (int_format(v) if type(locals[v]) == int else default_format(v)) for v in var_list]
    return out_list


def format_num_params(num_params: int, round_to_digits: int = 1) -> str:
    if num_params < 1_000:
        pnum = str(round(num_params, max(0, round_to_digits)))
        scalar = ""
    elif num_params < 1_000_000:
        pnum = f"{round(num_params/1_000, max(0, round_to_digits))}"
        scalar = "k"
    elif num_params < 1_000_000_000:
        pnum = f"{round(num_params/1_000_000, max(0, round_to_digits))}"
        scalar = "M"
    else:
        pnum = f"{round(num_params/1_000_000_000, max(0, round_to_digits))}"
        scalar = "B"

    before_dot = pnum.split(".")[0]
    after_dot = pnum.split(".")[1] if "." in pnum else ""
    after_dot = "" if after_dot and (round_to_digits <= 0) else after_dot
    after_dot = "" if after_dot and (int(after_dot) == 0) else after_dot
    after_dot = "." + after_dot if after_dot else ""

    return f"{before_dot}{after_dot}{scalar}"


########################################
#           Train and Eval             #
########################################

@torch.no_grad()
def calc_pplx(loss: torch.Tensor | float) -> torch.Tensor | float:
    return 2.71828 ** loss


@torch.no_grad()
def _eval_causal(
        net: SpeedyLangNet,
        eval_batchsize: int,
        num_eval_steps: int,
):
    # float32 here to prevent truncation errors
    val_loss, val_acc = torch.tensor(0., device=hyp['misc']['device'], dtype=torch.float), torch.tensor(0., device=hyp['misc']['device'], dtype=torch.float)
    
    for _ in range(num_eval_steps):
        sequence = get_batch(data, key='eval', batchsize=eval_batchsize, length=hyp['misc']['sequence_length']['max'])

        inputs, targets = get_causal_data(sequence)
        outputs = net(inputs)
        val_loss += 1./num_eval_steps * loss_fn(outputs.flatten(0, 1).float(), targets.flatten(0, 1))
        val_acc  += 1./num_eval_steps * (outputs.argmax(-1) == targets).float().mean()

    val_pplx = calc_pplx(val_loss)
    return val_loss.item(), val_acc.item(), val_pplx.item()


@torch.no_grad()
def _eval_plan_act(
        net: SpeedyLangNet,
        eval_batchsize: int,
        num_eval_steps: int,
        first_acting_token_idx: int,
        last_acting_token_idx: int,
        top_k: int,
):
    val_loss_planning, val_acc_planning = torch.tensor(0., device=hyp['misc']['device'], dtype=torch.float), torch.tensor(0., device=hyp['misc']['device'], dtype=torch.float)
    val_loss_acting_full, val_acc_acting_full = torch.tensor(0., device=hyp['misc']['device'], dtype=torch.float), torch.tensor(0., device=hyp['misc']['device'], dtype=torch.float)
    val_loss_acting_causal, val_acc_acting_causal = torch.tensor(0., device=hyp['misc']['device'], dtype=torch.float), torch.tensor(0., device=hyp['misc']['device'], dtype=torch.float)
    val_loss_acting_acting, val_acc_acting_acting = torch.tensor(0., device=hyp['misc']['device'], dtype=torch.float), torch.tensor(0., device=hyp['misc']['device'], dtype=torch.float)
    val_loss_acting_planning, val_acc_acting_planning = torch.tensor(0., device=hyp['misc']['device'], dtype=torch.float), torch.tensor(0., device=hyp['misc']['device'], dtype=torch.float)

    for _ in range(num_eval_steps):
        sequence = get_batch(data, key='eval', batchsize=eval_batchsize, length=hyp['misc']['sequence_length']['max'])

        inputs, targets = get_planning_data(sequence, first_acting_token_idx)
        outputs = net(inputs)
        val_loss_planning += 1./num_eval_steps * loss_fn(outputs.flatten(0, 1).float(), targets.flatten(0, 1))
        val_acc_planning += 1./num_eval_steps * (outputs.argmax(-1) == targets).float().mean()

        inputs, targets = get_acting_data(
            net=net,
            sequence=sequence,
            planning_output=outputs,
            first_acting_token_idx=first_acting_token_idx,
            last_acting_token_idx=last_acting_token_idx,
            top_k=top_k,
        )
        outputs = net(
            inputs, 
            first_acting_token_idx=first_acting_token_idx,
            last_acting_token_idx=last_acting_token_idx,    
        )
        val_loss_acting_full += 1./num_eval_steps * loss_fn(outputs.flatten(0, 1).float(), targets.flatten(0, 1))
        val_acc_acting_full += 1./num_eval_steps * (outputs.argmax(-1) == targets).float().mean()
        val_loss_acting_causal += 1./num_eval_steps * loss_fn(
            outputs[:, :first_acting_token_idx].flatten(0, 1).float(), 
            targets[:, :first_acting_token_idx].flatten(0, 1),
        )
        val_acc_acting_causal += 1./num_eval_steps * (
            outputs[:, :first_acting_token_idx].argmax(-1) 
            == targets[:, :first_acting_token_idx]
        ).float().mean()
        val_loss_acting_acting += 1./num_eval_steps * loss_fn(
            outputs[:, first_acting_token_idx:last_acting_token_idx].flatten(0, 1).float(), 
            targets[:, first_acting_token_idx:last_acting_token_idx].flatten(0, 1),
        )
        val_acc_acting_acting += 1./num_eval_steps * (
            outputs[:, first_acting_token_idx:last_acting_token_idx].argmax(-1) 
            == targets[:, first_acting_token_idx:last_acting_token_idx]
        ).float().mean()
        val_loss_acting_planning += 1./num_eval_steps * loss_fn(
            outputs[:, last_acting_token_idx:].flatten(0, 1).float(), 
            targets[:, last_acting_token_idx:].flatten(0, 1),
        )
        val_acc_acting_planning += 1./num_eval_steps * (
            outputs.argmax(-1)[:, last_acting_token_idx:]
            == targets[:, last_acting_token_idx:]
        ).float().mean()

    val_pplx_planning = calc_pplx(val_loss_planning)
    val_pplx_acting_full = calc_pplx(val_loss_acting_full)
    val_pplx_acting_causal = calc_pplx(val_loss_acting_causal)
    val_pplx_acting_acting = calc_pplx(val_loss_acting_acting)
    val_pplx_acting_planning = calc_pplx(val_loss_acting_planning)

    return (
        val_loss_planning, val_acc_planning, val_pplx_planning,
        val_loss_acting_full, val_acc_acting_full, val_pplx_acting_full,
        val_loss_acting_causal, val_acc_acting_causal, val_pplx_acting_causal,
        val_loss_acting_acting, val_acc_acting_acting, val_pplx_acting_acting,
        val_loss_acting_planning, val_acc_acting_planning, val_pplx_acting_planning,
    )


@torch.no_grad()
def quick_evaluation(net: SpeedyLangNet):
    net.eval()
    
    eval_batchsize           = max(math.floor(tokens_per_batch_capacity/(hyp['misc']['sequence_length']['max'])//16), 1) # Number of sequences per batch relative to the max-length batchsize capacity, downscale factor hardcoded to help prevent OOMs. Tunable
    num_eval_sequences       = hyp['opt']['num_eval_tokens']//hyp['misc']['sequence_length']['max']
    num_eval_steps           = num_eval_sequences//eval_batchsize

    causal_loss, causal_acc, causal_pplx = _eval_causal(
        net=net,
        eval_batchsize=eval_batchsize,
        num_eval_steps=num_eval_steps,
    )

    first_acting_token_idx, last_acting_token_idx = get_first_and_last_acting_token_idx(
        seq_len=max_sequence_length,
        planning_rate=0.25,
        acting_rate=0.01,
    )
    (
        val_loss_planning, val_acc_planning, val_pplx_planning,
        val_loss_acting_full, val_acc_acting_full, val_pplx_acting_full,
        _, _, _, _, _, _, _, _, _,
    ) = _eval_plan_act(
        net=net,
        eval_batchsize=eval_batchsize,
        num_eval_steps=num_eval_steps,
        first_acting_token_idx=first_acting_token_idx,
        last_acting_token_idx=last_acting_token_idx,
        top_k=5
    )

    net.train()

    return (
        causal_loss, causal_acc, causal_pplx,
        val_loss_planning, val_acc_planning, val_pplx_planning,
        val_loss_acting_full, val_acc_acting_full, val_pplx_acting_full,
    )


@torch.no_grad()
def full_evaluation(net: SpeedyLangNet):
    net.eval()
    
    eval_batchsize           = max(math.floor(tokens_per_batch_capacity/(hyp['misc']['sequence_length']['max'])//16), 1) # Number of sequences per batch relative to the max-length batchsize capacity, downscale factor hardcoded to help prevent OOMs. Tunable
    num_eval_sequences       = hyp['opt']['num_eval_tokens']//hyp['misc']['sequence_length']['max']
    num_eval_steps           = num_eval_sequences//eval_batchsize

    causal_loss, causal_acc, causal_pplx = _eval_causal(
        net=net,
        eval_batchsize=eval_batchsize,
        num_eval_steps=num_eval_steps,
    )
    results = {
        "setting": ["causal"], 
        "loss": [causal_loss], 
        "acc": [causal_acc], 
        "pplx": [causal_pplx],
        "loss_planning": [None],
        "acc_planning": [None],
        "pplx_planning": [None],
        "loss_acting_full": [None],
        "acc_acting_full": [None],
        "pplx_acting_full": [None],
        "loss_acting_causal": [None],
        "acc_acting_causal": [None],
        "pplx_acting_causal": [None],
        "loss_acting_acting": [None],
        "acc_acting_acting": [None],
        "pplx_acting_acting": [None],
        "loss_acting_planning": [None],
        "acc_acting_planning": [None],
        "pplx_acting_planning": [None],
    }
    
    acting_mask_widths = range(1, 11)
    last_acting_token_indices = range(13, max_sequence_length, step=10)
    top_ks = [1, 2, 3, 4, 5]

    for acting_mask_width, last_acting_token_idx, top_k in itertools.product(
        acting_mask_widths, last_acting_token_indices, top_ks
    ):
        first_acting_token_idx = last_acting_token_idx - acting_mask_width

        (
            val_loss_planning, val_acc_planning, val_pplx_planning,
            val_loss_acting_full, val_acc_acting_full, val_pplx_acting_full,
            val_loss_acting_causal, val_acc_acting_causal, val_pplx_acting_causal,
            val_loss_acting_acting, val_acc_acting_acting, val_pplx_acting_acting,
            val_loss_acting_planning, val_acc_acting_planning, val_pplx_acting_planning,
        ) = _eval_plan_act(
            net=net,
            eval_batchsize=eval_batchsize,
            num_eval_steps=num_eval_steps,
            first_acting_token_idx=first_acting_token_idx,
            last_acting_token_idx=last_acting_token_idx,
            top_k=top_k
        )

        results["setting"].append(str((first_acting_token_idx, last_acting_token_idx)))
        results["loss"].append(None)
        results["acc"].append(None)
        results["pplx"].append(None)
        results["loss_planning"].append(val_loss_planning)
        results["acc_planning"].append(val_acc_planning)
        results["pplx_planning"].append(val_pplx_planning)
        results["loss_acting_full"].append(val_loss_acting_full)
        results["acc_acting_full"].append(val_acc_acting_full)
        results["pplx_acting_full"].append(val_pplx_acting_full)
        results["loss_acting_causal"].append(val_loss_acting_causal)
        results["acc_acting_causal"].append(val_acc_acting_causal)
        results["pplx_acting_causal"].append(val_pplx_acting_causal)
        results["loss_acting_acting"].append(val_loss_acting_acting)
        results["acc_acting_acting"].append(val_acc_acting_acting)
        results["pplx_acting_acting"].append(val_pplx_acting_acting)
        results["loss_acting_planning"].append(val_loss_acting_planning)
        results["acc_acting_planning"].append(val_acc_acting_planning)
        results["pplx_acting_planning"].append(val_pplx_acting_planning)

    net.train()
    return results


def train(net: SpeedyLangNet | None = None, **settings):

    #################
    #     Init      #
    #################

    # Get network
    net = net or make_net(settings)

    # Init wandb 
    # TODO: update run name with the new options
    # TODO: use same run name for full eval at the end
    if settings['log_wandb']:
        wandb.finish()  # Finish any previous runs
        wandb.init(
            project=settings['wandb_project'], 
            config=settings,
            name=get_run_name(
                depth=settings['depth'],
                width=settings['width'],
                num_heads=settings['num_heads'],
                linear_value=settings['linear_value'],
                plan_act=settings['plan_act'],
                planning_divider=settings['planning_divider'],
                acting_divider=settings['acting_divider'],
                randomize_masking_rate=settings['randomize_masking_rate'],
                top_k=settings['top_k']
            ),
        )

    # Full-run statistics variables
    t_secs        = 0.
    curr_microbatch_step = curr_step = 0
    tokens_seen          = 0

    # Microbatch growing parameters
    # Leaving this hardcoded for now for simplicity, this helps keep the learning process stable.
    microbatch_steps = 0. # The noninteger estimate of microbatches required based upon the grad norm (sampled by dithering at each step.)
    discrete_sampled_microbatch_steps = max(1, int(microbatch_steps))

    # Start at the initial length and maximum allowable batchsize. The batchsize is adjusted so that we see roughly the same number of tokens per batch. This means that shorter sequence lengths will have much larger batch sizes.
    curr_length     = hyp['misc']['sequence_length']['initial']
    curr_batchsize  = tokens_per_batch_capacity // hyp['misc']['sequence_length']['initial']
    final_batchsize = tokens_per_batch_capacity /  hyp['misc']['sequence_length']['max']
    assert final_batchsize > 1, f"Error: Specified configuration takes up too much memory (calculated final batchsize {final_batchsize} is less than 1!)"

    # Validation parameters
    val_loss_causal, val_acc, val_pplx = None, None, None

    # Get the total number of parameters in our model and use that to generate/calculate the base lr.
    total_trainable_params = sum([p.data.numel() if p.requires_grad else 0 for p in net.parameters()])

    print('-'*(40))
    print(f"total trainable params: {total_trainable_params:,}")
    print('-'*(40))

    # Briefly log some details up front. (TODO: Condense nicely later.)
    print("curr_batchsize:     ", curr_batchsize)
    print("final_batchsize:    ", tokens_per_batch_capacity // hyp['misc']['sequence_length']['max'])
    print("max_sequence_length:", max_sequence_length)


    #####################
    # Scaling Equations #
    #####################

    # These equations are a result of rough general exponential/power law fits between parameters that worked for the 46M and 1.5B run
    # They seem to transfer not too badly when interpolating, however, they're far from perfect and assume 40 GB of memory (so if you use)
    # a smaller card, you might struggle a bit here. All in all -- this is still in alpha, but seems to be very useful within a limited arena
    # of making arbitrary models between 45M and 1.5B

    # A very, very pared down version of the gpt-3 training lr scaling rule roughly fit. It's used as a loose general base for the run LRs.
    base_lr = 9e7 / math.log(total_trainable_params)**8.8

    # The base value that we raise to the value of our loss in order to determine how much weight decay we need (exponentially strong as we approach 0.)
    weight_decay_pow_base = .007 * ((.01 * math.log(total_trainable_params))) ** (-4)

    # This defines how quickly we expect grad_norm drops for microbatch scheduling -- slightly faster for smaller models, slightly slower for larger models
    # Note: This will interact with really aggressive weight decay, some training runs may slow down a lot near the end as a result.
    microbatch_expected_grad_norm_pow = -.677 * math.log(total_trainable_params) ** -.2

    # Bit of a strange approximation, but this seemed
    microbatch_grad_norm_steps_scale = math.log(total_trainable_params) * total_trainable_params

    # Create multiple parameter groups based on parameter name, as certain kinds of parameters seem to work best
    # with specific combinations of learning rates and schedulers
    param_groups_dict = init_param_groups_dict(net, base_lr)
    opt               = torch.optim.AdamW(param_groups_dict.values(), fused=True)
    scheduler         = torch.optim.lr_scheduler.LambdaLR(opt, [k['scheduler'] for k in param_groups_dict.values()])

    # Save some results
    train_losses, val_losses_causal, train_accs, val_accs_causal, train_pplxs, val_pplxs_causal = [], [], [], [], [], []
    val_losses_planning, val_accs_planning, val_pplxs_planning = [], [], []
    val_losses_acting, val_accs_acting, val_pplxs_acting = [], [], []
    grad_norms, cumulative_time = [], []
    tokens_seen_list, epochs_list = [], []
    batch_sizes = []
    sequence_lengths = []
    learning_rates, weight_decays = [], []

    #################
    # Training Mode #
    #################

    ## print out the training column headers before each run.
    print_training_details(variables_to_log, column_labels_only=True)

    ## For accurately timing GPU code
    starter, ender = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
    torch.cuda.synchronize() ## clean up any pre-net setup operations
    starter.record()

    net.train()

    stop_run = False

    # Main loop. Most of the complexity here is in the dynamic growing scheduler(s).
    while True:
        sequence = get_batch(data, key='train', batchsize=curr_batchsize, length=curr_length)

        if settings['plan_act']:
            planner_masking_rate = settings['planner_masking_rate']
            actor_masking_rate = settings['actor_masking_rate']

            if settings['randomize_masking_rate']:
                planner_masking_rate = randomize_masking_rate(planner_masking_rate)
                actor_masking_rate = randomize_masking_rate(actor_masking_rate)
            actor_masking_rate = min(actor_masking_rate, planner_masking_rate - (1.1/curr_length))  # at least 1 token less than the planner

            first_acting_token_idx, last_acting_token_idx = get_first_and_last_acting_token_idx(
                seq_len=curr_length,
                planning_rate=planner_masking_rate,
                acting_rate=actor_masking_rate,
            )

            inputs, targets = get_planning_data(sequence, first_acting_token_idx=first_acting_token_idx)
            outputs = net(inputs)
            loss_planning = loss_fn(outputs.flatten(0, 1), targets.flatten(0, 1)) / settings["planning_divider"]

            inputs, targets = get_acting_data(
                net, sequence, outputs,
                first_acting_token_idx=first_acting_token_idx,
                last_acting_token_idx=last_acting_token_idx,
                top_k=settings['top_k'],
            )
            outputs = net(
                inputs, 
                first_acting_token_idx=first_acting_token_idx,
                last_acting_token_idx=last_acting_token_idx,
            )
            loss_acting = loss_fn(outputs.flatten(0, 1), targets.flatten(0, 1)) / settings["acting_divider"]

            loss = loss_planning + loss_acting
            loss.div(discrete_sampled_microbatch_steps).backward()
        else:
            inputs, targets = get_causal_data(sequence)
            outputs = net(inputs)
            loss = loss_fn(outputs.flatten(0, 1), targets.flatten(0, 1))
            loss.div(discrete_sampled_microbatch_steps).backward()

        tokens_seen += curr_batchsize * curr_length
        epoch = tokens_seen/len(data['train'])

        do_eval = curr_step % 10 == 0 and curr_microbatch_step % discrete_sampled_microbatch_steps == 0
            
        if (
                curr_step >= settings['max_steps'] 
                or epoch >= settings['max_epochs'] 
                or tokens_seen >= settings['max_tokens']
                or t_secs >= settings['max_time_seconds']
        ):
            do_eval=True
            stop_run = True

        # Quick non-eval summary every N training steps, at the end of every microbatch group, including when we are not doing a _full eval_ here so that the resulting stats are complete
        if do_eval:
            train_acc          = (outputs.detach().argmax(-1) == targets).float().mean().item()
            train_loss         = loss.detach().cpu().item()

            grad_norm = get_grad_norm(net)

            train_losses.append(train_loss)
            train_accs.append(train_acc)
            train_pplxs.append(float(calc_pplx(train_loss)))  # unnecessary float, but better safe than sorry
            grad_norms.append(grad_norm)
            tokens_seen_list.append(tokens_seen)
            epochs_list.append(epoch)
            batch_sizes.append(curr_batchsize)
            sequence_lengths.append(curr_length)
            cumulative_time.append(t_secs)
            learning_rates.append(opt.param_groups[0]['lr'])
            weight_decays.append(opt.param_groups[0]['weight_decay'])


        # Once we've accumulated steps over all of our microbatches, take a single full-batchsize step.
        if curr_microbatch_step % discrete_sampled_microbatch_steps == 0:
            # Step the optimizer, then scheduler
            opt.step()

            # Dynamic weight decay scheduling. Based upon something similar to the reciprocal of the perplexity of the network over the data [inspired by section 5 of https://arxiv.org/pdf/2204.02311.pdf]
            # Smaller models have a higher base, and weight decay kicks in more sharply later. For larger models, it activates more early
            opt.param_groups[0]['weight_decay'] = 1./weight_decay_pow_base**(loss.detach()+1e-8).item() * hyp['opt']['weight_decay']
            scheduler.step()

            # Check if we need to double our sequence length
            if curr_step % hyp['misc']['sequence_length']['growth_steps'] == 0 and curr_step != 0 and curr_length < hyp['misc']['sequence_length']['max']:
                curr_length, curr_batchsize = grow_sequence_length(curr_length, curr_batchsize)

            # The next several lines calculate a dynamic batchsize, simulated through manual dithering
            # There could be improvements or losses in changing the dithering strategy, since determinism and gradient descent can lead to some very not-so-nice (and subtle) loss oscillations.
            if curr_step % hyp['opt']['microbatch']['sample_every'] == 0:
                grad_norm = get_grad_norm(net)

                grad_norm_per_param = grad_norm/(total_trainable_params**.5) # This should keep the expected grad norm per parameter roughly the same (ignoring initializations) unless I did my napkin math wrong (feel free to correct it and test it out if so! <3 :') )
                grad_norm_target    = (((microbatch_grad_norm_steps_scale * (curr_step + 1e-2))) ** microbatch_expected_grad_norm_pow)
                ratio_diff          = grad_norm_per_param/(grad_norm_target)

                # Update the fractional number of steps based on the % difference between the grad norm and expected grad norm.
                microbatch_steps *= 1. + (hyp['opt']['microbatch']['sample_every'] * hyp['opt']['microbatch']['scale_lr'] * (ratio_diff - 1))
                microbatch_steps  = max(microbatch_steps, 1e-1) # Clamp to keep this from going to zero, so that we can bounce back if needed

            # simple bernoulli dithering with probabilities based on how close we are to each integer
            base, dither_prob = divmod(microbatch_steps, 1)

            # Randomly sample next accumulate steps to use. This is the dithered operation, the 'microbatch_steps' is the noninteger accumulator between steps.
            discrete_sampled_microbatch_steps = max(1, int(base + torch.bernoulli(torch.tensor(dither_prob)).item())) # bernoulli via torch to save an unnecesary import :)

            opt.zero_grad()

            # reset microbatch steps and increment current step
            curr_microbatch_step = 0
            curr_step += 1

        if do_eval:
            ender.record()
            torch.cuda.synchronize()

            t_secs += 1e-3 * starter.elapsed_time(ender)
            train_loss = loss.detach().cpu().item() # Update the loss for the training details printout

            (
                val_acc, val_loss_causal, val_pplx,
                val_loss_planning, val_acc_planning, val_pplx_planning,
                val_loss_acting, val_acc_acting, val_pplx_acting,
            ) = quick_evaluation(net)

            val_losses_causal.append(val_loss_causal)
            val_accs_causal.append(val_acc)
            val_pplxs_causal.append(val_pplx)
            val_losses_planning.append(val_loss_planning)
            val_accs_planning.append(val_acc_planning)
            val_pplxs_planning.append(val_pplx_planning)
            val_losses_acting.append(val_loss_acting)
            val_accs_acting.append(val_acc_acting)
            val_pplxs_acting.append(val_pplx_acting)
            
            
            if settings['log_wandb']:
                wandb.log({
                    'train/loss': train_loss,
                    'train/acc': train_acc,
                    'train/pplx': calc_pplx(train_loss),
                    'val/loss/causal': val_loss_causal, 
                    'val/acc/causal': val_acc, 
                    'val/pplx/causal': val_pplx,
                    'val/loss/planning': val_loss_planning,
                    'val/acc/planning': val_acc_planning,
                    'val/pplx/planning': val_pplx_planning,
                    'val/loss/acting': val_loss_acting,
                    'val/acc/acting': val_acc_acting,
                    'val/pplx/acting': val_pplx_acting,
                    'tokens_seen': tokens_seen, 
                    'epoch': epoch,
                    'batch_size': curr_batchsize,
                    'sequence_length': curr_length,
                    'cumulative_time': t_secs,
                    'learning_rate': opt.param_groups[0]['lr'],
                    'weight_decay': opt.param_groups[0]['weight_decay'],
                })

            # Print out our training details
            ## We also check to see if we're on our final eval loop (assum that max_curr_step lines up with the eval_every value) so we can print the 'bottom' of the table for each round.
            print_training_details(format_for_table(variables_to_log, locals=locals()), is_final_entry=stop_run)

            torch.cuda.synchronize()
            starter.record()
            net.train()
        curr_microbatch_step += 1
        if stop_run:
            break

    return (
        net, val_loss_causal,
        train_losses, train_pplxs, train_accs,
        val_losses_causal, val_accs_causal, val_pplxs_causal,
        val_losses_planning, val_accs_planning, val_pplxs_planning,
        val_losses_acting, val_accs_acting, val_pplxs_acting,
        grad_norms, cumulative_time, 
        tokens_seen_list, epochs_list,
        batch_sizes, sequence_lengths, learning_rates, weight_decays,
    )


def get_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train a model on a dataset.")

    # DEFINE ARGS
    # Logging
    parser.add_argument(
        "-c", "--log_csv", 
        action="store_true", 
        help="Log results to csv-file. FLAG"
    )
    parser.add_argument(
        "--append", 
        action="store_true", 
        help="If set, the logfile won't be overwritten but appended to, if it already exists. FLAG"
    )
    parser.add_argument(
        "--logfile", 
        type=str,
        default="results_041.csv", 
        help="Log the results to this file. "
        "TYPE: str; DEFAULT: 'results_041.csv'"
    )
    parser.add_argument(
        "-w", "--log_wandb", 
        action="store_true", 
        help="Log results to Weights & Biases. FLAG"
    )
    parser.add_argument(
        "--wandb_project",
        type=str, default="speedy-lang", 
        help="Weights & Biases project to log to."
        "TYPE: str; DEFAULT: 'speedy-lang'"
    )

    # How many runs per setting, how many steps/epochs/tokens to train/validate for per run
    parser.add_argument(
        "--num_runs", 
        type=int, default=1, 
        help="Number of times to run each experiment for. "
        "Each run for a single setting will start with a different seed, "
        "but over the different settings, the seeds are repeated run-by-run to get comparable results. "
        "TYPE: int; DEFAULT: 1"
    )
    parser.add_argument(
        "--max_steps", 
        type=int, default=int(1e9), 
        help="If step>=max_steps, stop training and eval one last time. "
        "Very high by default so that epochs are the determining factor by default. "
        "One step does *not* correspond to a constant number of tokens, "
        "as the batch size and sequence length are adjusted dynamically. "
        "TYPE: int; DEFAULT: int(1e9)"
    )
    parser.add_argument(
        "--max_epochs", 
        type=float, default=1, 
        help="If epoch>=max_epochs, stop training and eval one last time. "
        "By default, this is the determining factor for training length. "
        "TYPE: int; DEFAULT: 1"
    )
    parser.add_argument(
        "--max_tokens", 
        type=int, default=int(1e12), 
        help="If token>=max_tokens, stop training and eval one last time. "
        "Very high by default so that epochs are the determining factor by default. "
        "TYPE: int; DEFAULT: int(1e12)"
    )
    parser.add_argument(
        "--max_time_seconds",
        type=int, default=int(1e9),
        help="If t_secs>=max_time_seconds, stop training and eval one last time. "
        "Very high by default so that epochs are the determining factor by default. "
        "TYPE: int; DEFAULT: int(1e9)"
    )

    # Model settings
    parser.add_argument(
        "--model_scale", 
        type=float, default=1.0, nargs="+", 
        help="Scale the model size. Can be overwritten by setting depth and width. "
        "You can provide multiple values to test multiple scales. "
        "TYPE: float; DEFAULT: 1.0"
    )
    parser.add_argument(
        "--depth", 
        type=int, default=-1, nargs="+", 
        help="Depth of the model. If <1, will be automatically determined via model_scale. "
        "You can provide multiple values to test multiple depths. "
        "TYPE: int; DEFAULT: -1"
    )
    parser.add_argument(
        "--width", 
        type=int, default=-1, nargs="+",
        help="Width of the model. If <1, will be automatically determined via model_scale. "
        "Will be automatically rounded to the nearest multiple of 64. "
        "You can provide multiple values to test multiple widths. "
        "TYPE: int; DEFAULT: -1"
    )
    parser.add_argument(
        "--num_heads", 
        type=int, default=1, nargs="+", 
        help="Number of attention heads. "
        "The original implementation is single-headed, but this might prove valuable for some experiments. "
        "You can provide multiple values to test multiple numbers of heads. "
        "TYPE: int; DEFAULT: 1"
    )
    parser.add_argument(
        "--linear_value",
        type=int, default=0, nargs="+",
        help=
        "If 0, use Gelu on the value in attention (the default setting of this package), else don't. "
        "If you provide several values (for example, 0 1 2 3 4), "
        "will be reduced to their booleans without repetition (so False, True). "
        "TYPE: int; DEFAULT: 0"
    )

    # Other settings
    parser.add_argument(
        "--gpu_capacity_scalar", 
        type=float, default=1.0, 
        help="1.0 is for a 40GB A100; reduce or increase as needed. You may need to include some slack. "
        "TYPE: float; DEFAULT: 1.0"
    )
    parser.add_argument(
        "--seed", 
        type=int, default=100, 
        help="Seed for the random number generator. "
        "This determines the initial seed per experiment. "
        "At each run, 1 is added to the seed, until the next setting. "
        "For example: you have two settings and 3 runs each, with an initial seed of 100. "
        "Then the seeds for the 3 runs of setting 1 will be [100, 101, 102], "
        "and the seeds for the 3 runs of setting 2 will be identical to make them comparable. "
        "TYPE: int; DEFAULT: 100"
    )
    parser.add_argument(
        "--review_settings",
        action="store_true",
        help="Print the settings before proceeding to review them. "
        "Useful because some settings might be pre-filtered "
        "(for example, if you have different widths and num_heads, "
        "only the combinations where width is divisible by num_heads are used). "
        "If something is wrong with the settings, you can easily see it here, return early, and fix it. FLAG"
    )

    # Custom settings
    parser.add_argument(
        "--plan_act",
        action="store_true",
        help="Use the plan-act task during training. FLAG"
    )
    parser.add_argument(
        "--planning_divider",
        type=float, default=2.0, nargs="+",
        help="Divider for the planning loss. TYPE: float; DEFAULT: 2.0"
    )
    parser.add_argument(
        "--acting_divider",
        type=float, default=2.0, nargs="+",
        help="Divider for the acting loss. TYPE: float; DEFAULT: 2.0"
    )
    parser.add_argument(
        "--loss_divider_method",
        type=str, choices=["zip", "product"], default="zip",
        help="How to combine the different loss dividers. "
        "If 'zip', the dividers are zipped together and used for each loss."
        " If zip, all four args must have the same length. "
        "If 'product', every possible combination of dividers is used for a setting once. "
        "TYPE: str; DEFAULT: 'zip'"
    )
    parser.add_argument(
        "--planner_masking_rate",
        type=float, default=0.25,
        help="Masking rate for the planning task. TYPE: float; DEFAULT: 0.25"
    )
    parser.add_argument(
        "--actor_masking_rate",
        type=float, default=0.1,
        help="Masking rate for the acting task. TYPE: float; DEFAULT: 0.1"
    )
    parser.add_argument(
        "--randomize_masking_rate",
        action="store_true",
        help="If this flag is set, the masking rates will be randomized "
        "using a Beta-distribution with concentration=8 around the given masking rates. "
        "FLAG"
    )
    parser.add_argument(
        "--top_k",
        type=int, default=5,
        help="Top-k for the acting task. TYPE: int; DEFAULT: 5"
    )

    # PARSE ARGS
    args = parser.parse_args()

    # CHECK & PREPROCESS ARGS
    args.depth = [args.depth] if isinstance(args.depth, int) else args.depth
    args.width = [args.width] if isinstance(args.width, int) else args.width
    args.depth = [None if d < 1 else d for d in args.depth]
    args.width = [None if w < 1 else w for w in args.width]
    args.num_heads = [args.num_heads] if isinstance(args.num_heads, int) else args.num_heads
    args.planning_divider = [args.planning_divider] if isinstance(args.planning_divider, float) else args.planning_divider
    args.acting_divider = [args.acting_divider] if isinstance(args.acting_divider, float) else args.acting_divider

    args.model_scale = [args.model_scale] if isinstance(args.model_scale, float) else args.model_scale
    args.linear_value = [args.linear_value] if isinstance(args.linear_value, int) else args.linear_value
    args.linear_value = list(set([bool(v) for v in args.linear_value]))

    if args.plan_act and args.loss_divider_method == "zip" and len(args.planning_divider) != len(args.acting_divider):
        raise ValueError("If loss_divider_method is 'zip', all dividers must have the same length.")

    if any(d is None or w is None for d in args.depth for w in args.width):
        assert all(d is None and w is None for d in args.depth for w in args.width), (
            "Set either both depth and width (all values >= 1), or neither (all values < 1)."
        )
        assert all(m > 0 for m in args.model_scale), "Please set a positive model_scale"
    else:
        print("\n[WARNING] Scaling by depth and width explicitly. Ignoring model_scale (will be automatically determined) [WARNING]\n")

    # PRINT ARGS --> CHECK IF EVERYTHING WORKED AS INTENDED
    print(args.__dict__)

    return args


def setting_violates_rules(**setting) -> bool:
    # You can add any rules here that you want to filter out.

    # Filter out all settings where the width is not divisible by the number of heads
    width = setting['width'] or to_nearest_64(384 * math.log2(1.+setting['model_scale']))
    if width % setting["num_heads"] != 0:
        return True
    
    return False


def get_settings(args: argparse.Namespace) -> list:
    # You can filter the combinations of args here;
    # potentially, not all args should appear with all others,
    # and you can handle that here.

    settings =  list(itertools.product(
        args.model_scale, args.depth, args.width, args.num_heads, args.linear_value
    ))

    if args.plan_act and args.loss_divider_method == "product":
        settings_loss_dividers = list(itertools.product(
            args.planning_divider, args.acting_divider,
        )) 
    elif args.plan_act and args.loss_divider_method == "zip":
        settings_loss_dividers = list(zip(
            args.planning_divider, args.acting_divider, strict=True)
        )
    elif not args.plan_act:
        settings_loss_dividers = [(0., 0.)]

    settings = [
        (model_scale, depth, width, num_heads, linear_value, planning_divider, acting_divider) 
        for model_scale, depth, width, num_heads, linear_value in settings 
        for planning_divider, acting_divider in settings_loss_dividers
        if not setting_violates_rules(
            model_scale=model_scale, 
            depth=depth, 
            width=width, 
            num_heads=num_heads, 
            linear_value=linear_value,
        )
    ]

    return settings


def print_settings(settings: list[tuple], names: list[str] = None):
    assert len(names) == len(settings[0]), "Please provide all setting names to print_settings."
    title = ":" * 10 + " SETTINGS " + ":" * 10
    sep = ":" * len(title)
    print("\n\n" + sep + "\n" + title + "\n" + sep + "\n\n")
    for i, setting in enumerate(settings):
        named_settings = "\n".join([f"{n}={s}" for n, s in zip(names, setting)])
        print(f"Setting {i+1}/{len(settings)}:\n{named_settings}\n\n")


def get_run_name(
        depth: int,
        width: int,
        seed: int,
        num_heads: int,
        linear_value: bool,
        plan_act: bool,
        planning_divider: float,
        acting_divider: float,
        randomize_masking_rate: bool,
        top_k: int,
):
    run_name = f"depth_{depth}_width_{width}_seed_{seed}_num_heads_{num_heads}"
    if linear_value:
        run_name = "linear_value_" + run_name
    if plan_act:
        if randomize_masking_rate:
            run_name = "randomize_masking_rate_" + run_name
        run_name = (
            "plan-act_loss-dividers-P-A_"
            f"{planning_divider}-{acting_divider}_"
            f"top_k_{top_k}"
        ) + run_name

    return run_name


def main():
    args = get_args()
    settings = get_settings(args)

    if args.review_settings:
        print_settings(
            settings, names=[
                "model_scale", "depth", "width", "num_heads", "linear_value",
                "plan_act", "planning_divider", "acting_divider",
                "randomize_masking_rate", "top_k",
            ]
        )
        proceed = input("Proceed? [y/n] ")
        if proceed.lower() != "y":
            print("Aborting.")
            return

    cumulative_run_num = 0
    total_num_runs = int(len(settings) * args.num_runs)

    global hyp, model_scale
    change_gpu_token_capacity(args.gpu_capacity_scalar)

    for setting_num, (model_scale, depth, width, num_heads, linear_value, planning_divider, acting_divider) in enumerate(settings):
        seed = args.seed  # reset seed so that every setting goes through the same seeds over the different runs

        # Change the model scale; width is rounded to nearest 64, and both are None if scaled by model_scale -> get depth and width here
        num_params, num_non_embedding_params, depth, width = change_model_scale(model_scale, depth, width, num_heads)
        for run_num in range(args.num_runs):
            cumulative_run_num += 1

            # Print some feedback
            title = (
                f"::: STARTING RUN {cumulative_run_num}/{total_num_runs} "
                f"(Setting {setting_num+1}/{len(settings)}, Run {run_num+1}/{args.num_runs})"
                f"\n:::    {num_heads=}"
                f"\n:::    {linear_value=}"
                f"\n:::    {model_scale=:.4f}"
                f"\n:::    {depth=}"
                f"\n:::    {width=}"
                f"\n:::    num_params={format_num_params(num_params)}"
                f"\n:::    num_non_embedding_params={format_num_params(num_non_embedding_params)}"
                f"\n:::    plan_act={args.plan_act}"
                f"\n:::    {planning_divider=}"
                f"\n:::    {acting_divider=}"
                f"\n:::    planner_masking_rate={args.planner_masking_rate}"
                f"\n:::    actor_masking_rate={args.actor_masking_rate}"
                f"\n:::    randomize_masking_rate={args.randomize_masking_rate}"
                f"\n:::    top_k={args.top_k}"
            )
            max_len = max(len(line) for line in title.split("\n"))
            title = "\n".join([line + " " * (max_len - len(line)) + " :::" for line in title.split("\n")])
            sep = ":" * max(len(line) for line in title.split("\n"))
            title = "\n\n" + "\n".join([sep, title, sep]) + "\n\n"
            print(title)

            # Seed
            torch.manual_seed(seed)
            random.seed(seed)

            # Train
            (
                net, last_val_loss,
                train_losses, train_pplxs, train_accs,
                val_losses_causal, val_accs_causal, val_pplxs_causal,
                val_losses_planning, val_accs_planning, val_pplxs_planning,
                val_losses_acting, val_accs_acting, val_pplxs_acting,
                grad_norms, cumulative_times,
                tokens_seen_list, epochs_list,
                batch_sizes, sequence_lengths, learning_rates, weight_decays,
            ) = train(
                net=None,  # you can give this the net and it will just continue training on it
                depth=depth,
                width=width,
                num_heads=num_heads,
                linear_value=linear_value,
                max_epochs=args.max_epochs,
                max_steps=args.max_steps,
                max_tokens=args.max_tokens,
                max_time_seconds=args.max_time_seconds,
                log_wandb=args.log_wandb,
                wandb_project=args.wandb_project,
                # include everything you want to log to wandb below, even if it's not used in the training function
                num_params=num_params,
                num_non_embedding_params=num_non_embedding_params,
                model_scale=model_scale,
                gpu_token_capacity=gpu_token_capacity,
                tokens_per_batch_capacity=tokens_per_batch_capacity,
                max_sequence_length=max_sequence_length,
                seed=seed,
                plan_act=args.plan_act,
                planning_divider=planning_divider,
                acting_divider=acting_divider,
                randomize_masking_rate=args.randomize_masking_rate,
                top_k=args.top_k,
                planner_masking_rate=args.planner_masking_rate,
                actor_masking_rate=args.actor_masking_rate,
            )

            # TODO: if args.plan_act, do a full evaluation here; save it; save reference to it in results
            if args.plan_act:
                full_eval_results = full_evaluation(net)
                os.makedirs("results/full_evaluations", exist_ok=True)
                full_eval_path = "results/full_evaluations/" + get_run_name(
                    depth=depth,
                    width=width,
                    num_heads=num_heads,
                    linear_value=linear_value,
                    plan_act=args.plan_act,
                    planning_divider=planning_divider,
                    acting_divider=acting_divider,
                    randomize_masking_rate=args.randomize_masking_rate,
                    top_k=args.top_k,
                ) + ".csv"
                pl.DataFrame(full_eval_results).write_csv(full_eval_path)
            else:
                full_eval_path = None

            # You can do whatever you want with your net here; I delete it to save VRAM
            del net 

            # Save results
            results = {
                "last_val_loss": [last_val_loss],
                "plan_act": [args.plan_act],
                "planning_divider": [planning_divider],
                "acting_divider": [acting_divider],
                "randomize_masking_rate": [args.randomize_masking_rate],
                "top_k": [args.top_k],
                "randomize_denoiser_settings": [args.randomize_denoiser_settings],
                "randomize_mask_width": [args.randomize_mask_width],
                "model_scale": [model_scale],
                "depth": [hyp['net']['num_blocks']],
                "width": [hyp['net']['residual_depth']],
                "num_params": [num_params],
                "num_non_embedding_params": [num_non_embedding_params],
                "num_heads": [num_heads],
                "linear_value": [linear_value],
                "seed": [seed],
                "run_num": [run_num+1],
                "max_epochs": [args.max_epochs],
                "max_steps": [args.max_steps],
                "max_tokens": [args.max_tokens],
                "max_time_seconds": [args.max_time_seconds],
                "gpu_capacity_scalar": [args.gpu_capacity_scalar],
                "train_loss": [str(train_losses)],
                "train_pplx": [str(train_pplxs)],
                "train_acc": [str(train_accs)],
                "val_loss_causal": [str(val_losses_causal)],
                "val_acc_causal": [str(val_accs_causal)],
                "val_pplx_causal": [str(val_pplxs_causal)],
                "val_loss_planning": [str(val_losses_planning)],
                "val_acc_planning": [str(val_accs_planning)],
                "val_pplx_planning": [str(val_pplxs_planning)],
                "val_loss_acting": [str(val_losses_acting)],
                "val_acc_acting": [str(val_accs_acting)],
                "val_pplx_acting": [str(val_pplxs_acting)],
                "grad_norm": [str(grad_norms)],
                "cumulative_time": [str(cumulative_times)],
                "tokens_seen": [str(tokens_seen_list)],
                "epoch": [str(epochs_list)],
                "batch_size": [str(batch_sizes)],
                "seq_length": [str(sequence_lengths)],
                "learning_rate": [str(learning_rates)],
                "weight_decay": [str(weight_decays)],
                "full_evaluation_file": [full_eval_path],
            }
            df = pl.DataFrame(results)


            if args.log_csv:
                if not os.path.exists(args.logfile) or ((not args.append) and (run_num == setting_num == 0)):
                    df.write_csv(args.logfile)
                else:
                    with open(args.logfile, 'ab') as f:
                        df.write_csv(f, include_header=False)

            seed += 1


if __name__ == "__main__":
    main()
