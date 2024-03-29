from jax import numpy as jnp, random, jit, value_and_grad, nn
import flax
from commplax import util, comm, cxopt, op, optim
from commplax.module import core, layer
import numpy as np
from functools import partial
from collections import namedtuple
from tqdm.auto import tqdm
from typing import Any, Optional, Union
from . import data as gdat
import jax
import optax

Model = namedtuple('Model', 'module initvar overlaps name')
Array = Any
Dict = Union[dict, flax.core.FrozenDict]


def make_base_module(steps: int = 3,
                     dtaps: int = 261,
                     ntaps: int = 41,
                     rtaps: int = 61,
                     init_fn: tuple = (core.delta, core.gauss),
                     w0 = 0.,
                     mode: str = 'train'):

    _assert_taps(dtaps, ntaps, rtaps)

    d_init, n_init = init_fn

    if mode == 'train':
        # configure mimo to its training mode
        mimo_train = True
    elif mode == 'test':
        # mimo operates at training mode for the first 200000 symbols,
        # then switches to tracking mode afterwards
        mimo_train = cxopt.piecewise_constant([200000], [True, False])
    else:
        raise ValueError('invalid mode %s' % mode)
        
    base = layer.Serial(
        layer.FDBP(steps=steps,
                   dtaps=dtaps,
                   ntaps=ntaps,
                   d_init=d_init,
                   n_init=n_init),
        layer.BatchPowerNorm(mode=mode),
        layer.MIMOFOEAf(name='FOEAf',
                        w0=w0,
                        train=mimo_train,
                        preslicer=core.conv1d_slicer(rtaps),
                        foekwargs={}),
        layer.vmap(layer.Conv1d)(name='RConv', taps=rtaps),  # vectorize column-wise Conv1D
        layer.MIMOAF(train=mimo_train))  # adaptive MIMO layer
        
    return base


def _assert_taps(dtaps, ntaps, rtaps, sps=2):
    ''' we force odd taps to ease coding '''
    assert dtaps % sps, f'dtaps must be odd number, got {dtaps} instead'
    assert ntaps % sps, f'ntaps must be odd number, got {ntaps} instead'
    assert rtaps % sps, f'rtaps must be odd number, got {rtaps} instead'


def fdbp_init(a: dict,
              xi: float = 1.1,
              steps: Optional[int] = None):

    def d_init(key, shape, dtype=jnp.complex64):
        dtaps = shape[0]
        d0, _ = comm.dbp_params(
            a['samplerate'],
            a['distance'] / a['spans'],
            a['spans'],
            dtaps,
            a['lpdbm'] - 3,  # rescale as input power which has been norm to 2 in dataloader
            virtual_spans=steps)
        return d0[0, :, 0]

    def n_init(key, shape, dtype=jnp.float32):
        dtaps = shape[0]
        _, n0 = comm.dbp_params(
            a['samplerate'],
            a['distance'] / a['spans'],
            a['spans'],
            dtaps,
            a['lpdbm'] - 3,  # rescale
            virtual_spans=steps)

        return xi * n0[0, 0, 0] * core.gauss(key, shape, dtype)

    return d_init, n_init


def model_init(data: gdat.Input,
               base_conf: dict,
               sparams_flatkeys: list,
               n_symbols: int = 4000,
               sps : int = 2,
               name='Model'):
    
    mod = make_base_module(**base_conf, w0=data.w0)
    y0 = data.y[:n_symbols * sps]
    rng0 = random.PRNGKey(0)
    z0, v0 = mod.init(rng0, core.Signal(y0))
    ol = z0.t.start - z0.t.stop
    sparams, params = util.dict_split(v0['params'], sparams_flatkeys)
    state = v0['af_state']
    aux = v0['aux_inputs']
    const = v0['const']
    return Model(mod, (params, state, aux, const, sparams), ol, name)


def simclr_contrastive_loss(z1, z2, temperature=0.1, LARGE_NUM=1e9):
    batch_size = z1.shape[0]

    z1 = l2_normalize(z1, axis=1)
    z2 = l2_normalize(z2, axis=1)

    representations = jnp.vstack([z1, z2])

    similarity_matrix = jnp.matmul(representations, representations.T) / temperature

    similarity_matrix -= jnp.eye(2 * batch_size) * LARGE_NUM

    positives = jnp.exp(similarity_matrix[:batch_size, batch_size:]) / temperature
    negatives = jnp.sum(jnp.exp(similarity_matrix[:batch_size, :batch_size]) / temperature, axis=1) + \
                jnp.sum(jnp.exp(similarity_matrix[:batch_size, batch_size + 1:]) / temperature, axis=1)

    loss = -jnp.log(positives / (positives + negatives))
    loss = jnp.mean(loss)

    return loss

def l2_normalize(x, axis=None, epsilon=1e-12):
    square_sum = jnp.sum(jnp.square(x), axis=axis, keepdims=True)
    x_inv_norm = jnp.sqrt(jnp.maximum(square_sum, epsilon))
    return x / x_inv_norm
  
def negative_cosine_similarity(p, z):
    p = l2_normalize(p, axis=1)
    z = l2_normalize(z, axis=1)
    return -jnp.mean(jnp.sum(p * z, axis=1))

def apply_transform(x, scale_range=(0.5, 2.0), p=0.5):
    if np.random.rand() < p:
        scale = np.random.uniform(scale_range[0], scale_range[1])
        x = x * scale
    return x
  
def apply_transform1(x, shift_range=(-5.0, 5.0), p=0.5):
    if np.random.rand() < p:
        shift = np.random.uniform(shift_range[0], shift_range[1])
        x = x + shift
    return x

def apply_transform2(x, mask_range=(0, 30), p=0.5):
    if np.random.rand() < p:
        total_length = x.shape[0]
        mask = np.random.choice([0, 1], size=total_length, p=[1-p, p])
        mask = jnp.array(mask)[:, None]
        mask = jnp.broadcast_to(mask, x.shape)
        x = x * mask
    return x

def apply_transform3(x, range=(0.0, 0.2), p=0.5):
    if np.random.rand() < p:
        sigma = np.random.uniform(range[0], range[1])
        x = x + np.random.normal(0, sigma, x.shape)
    return x
  
def apply_transform4(x, range=(0.5, 30.0), band_width=2.0, sampling_rate=100.0, p=0.5):
    if np.random.rand() < p:
        low_freq = np.random.uniform(range[0], range[1])
        center_freq = low_freq + band_width / 2.0
        b, a = signal.iirnotch(center_freq, center_freq / band_width, fs=sampling_rate)
        x = signal.lfilter(b, a, x)
    return x
  
def loss_fn(module: layer.Layer,
            params: Dict,
            state: Dict,
            y: Array,
            x: Array,
            aux: Dict,
            const: Dict,
            sparams: Dict,):
    params = util.dict_merge(params, sparams)
    y_transformed = apply_transform(y)
    y_transformed1 = apply_transform1(y)
   
    z_original, updated_state = module.apply(
        {'params': params, 'aux_inputs': aux, 'const': const, **state}, core.Signal(y))
    z_transformed, _ = module.apply(
        {'params': params, 'aux_inputs': aux, 'const': const, **state}, core.Signal(y_transformed))
    z_transformed1, _ = module.apply(
        {'params': params, 'aux_inputs': aux, 'const': const, **state}, core.Signal(y_transformed1))
    
    # aligned_x = x[z_original.t.start:z_original.t.stop]
    # mse_loss = jnp.mean(jnp.abs(z_original.val - aligned_x) ** 2)
    z_original_real = jnp.abs(z_original.val)  
    z_transformed_real = jnp.abs(z_transformed.val) 
    z_transformed_real1 = jnp.abs(z_transformed1.val) 
    z_transformed1_real1 = jax.lax.stop_gradient(z_transformed_real1)
    contrastive_loss = negative_cosine_similarity(z_transformed_real, z_transformed_real1)
    # total_loss = mse_loss + 0.1 * contrastive_loss

    return contrastive_loss, updated_state

@partial(jit, backend='cpu', static_argnums=(0, 1))
# def update_step(module: layer.Layer,
#                 opt: cxopt.Optimizer,
#                 i: int,
#                 opt_state: tuple,
#                 module_state: Dict,
#                 y: Array,
#                 x: Array,
#                 aux: Dict,
#                 const: Dict,
#                 sparams: Dict):

#     params = opt.params_fn(opt_state)
#     (loss, module_state), grads = value_and_grad(
#         loss_fn, argnums=1, has_aux=True)(module, params, module_state, y, x,
#                                           aux, const, sparams)
#     opt_state = opt.update_fn(i, grads, opt_state)
#     return loss, opt_state, module_state

def update_step(module, optimizer, opt_state, params, module_state, y, x, aux, const, sparams):
  
    def compute_loss(params):
        loss, new_module_state = loss_fn(module, params, module_state, y, x, aux, const, sparams)
        return loss, new_module_state
      
    grads_fn = jax.value_and_grad(compute_loss, has_aux=True)
    (loss, new_module_state), grads = grads_fn(params)
  
    updates, new_opt_state = optimizer.update(grads, opt_state, params)
    new_params = optax.apply_updates(params, updates)
  
    return loss, new_opt_state, new_module_state, new_params

def get_train_batch(ds: gdat.Input,
                    batchsize: int,
                    overlaps: int,
                    sps: int = 2):
                      
    flen = batchsize + overlaps
    fstep = batchsize
    ds_y = op.frame_gen(ds.y, flen * sps, fstep * sps)
    ds_x = op.frame_gen(ds.x, flen, fstep)
    n_batches = op.frame_shape(ds.x.shape, flen, fstep)[0]
    return n_batches, zip(ds_y, ds_x)

def train(model: Model,
          data: gdat.Input,
          batch_size: int = 500,
          n_iter=None,
          optimizer = optax.chain(
    # optax.clip_by_global_norm(1.0),  # 梯度裁剪，限制全局梯度范数
    optax.scale_by_adam(),
    optax.scale_by_schedule(optax.exponential_decay(init_value=1e-4, 
                                                    transition_steps=1000, 
                                                    decay_rate=0.9)))):

    params, module_state, aux, const, sparams = model.initvar

    opt_state = optimizer.init(params)

    n_batch, batch_gen = get_train_batch(data, batch_size, model.overlaps)
    n_iter = n_batch if n_iter is None else min(n_iter, n_batch)

    for i, (y, x) in tqdm(enumerate(batch_gen), total=n_iter, desc='training', leave=False):
        if i >= n_iter: break
        aux = core.dict_replace(aux, {'truth': x})

        loss, opt_state, module_state, params = update_step(model.module, optimizer, opt_state, params, module_state, y, x, aux, const, sparams)
        yield loss, params, module_state



def test(model: Model,
         params: Dict,
         data: gdat.Input,
         eval_range: tuple=(300000, -20000),
         metric_fn=comm.qamqot):

    state, aux, const, sparams = model.initvar[1:]
    aux = core.dict_replace(aux, {'truth': data.x})
    if params is None:
      params = model.initvar[0]

    z, _ = jit(model.module.apply,
               backend='cpu')({
                   'params': util.dict_merge(params, sparams),
                   'aux_inputs': aux,
                   'const': const,
                   **state
               }, core.Signal(data.y))
    metric = metric_fn(z.val,
                       data.x[z.t.start:z.t.stop],
                       scale=np.sqrt(10),
                       eval_range=eval_range)
    return metric, z
