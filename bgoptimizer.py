# %%
%load_ext autoreload
%autoreload 2

import os
from xisf import XISF
import numpy as np
import tensorflow as tf
from tensorflow import keras
from spline import Spline

# For preprocessing
from skimage.measure import block_reduce

# For visualization only
import matplotlib.pyplot as plt
from PIL import Image 

# For experiments only
import pandas as pd 

# %%

# __/ Script parameters \__________
# TODO: command line args
base_dir = "S:\\src\\bg-model"
in_dir = "in\\orion"
in_filename = "O3.xisf" 
in_filepath = os.path.join(base_dir, in_dir, in_filename)
out_dir = "out\\orion"
out_filepath = os.path.join(base_dir, out_dir, in_filename)
bg_filepath = os.path.join(base_dir, out_dir, "bg_"+in_filename)

config = {
    'downscaling_factor': 8, 'downscaling_func': 'mean',
    'delinearization_quantile': 0.95,
    'N': 8,
    'O': 2,
    'threshold': 0.9,
    'B': 1,
    'alpha': 5,
    'lr': 0.001,
    'epochs': 10000,
}

# %%

# __/ Preprocessing \__________

# min, med, max
def statistics(im, title=""):
    im_min, im_med, im_max = np.nanmin(im), np.nanmedian(im), np.nanmax(im)
    print(f"[{title.ljust(12)}] Min / Median / Max = {im_min:.4f} / {im_med:.4f} / {im_max:.4f}", end='')
    print("  CLIPPING!" if im_min < 0 or im_max > 1 else "")
    return im_min, im_med, im_max


def plot_image_hist(im, title=""):
    fig, (ax0, ax1) = plt.subplots(2, 1, figsize=(16,12), gridspec_kw={'height_ratios': [4, 1]})
    _ = ax0.imshow(im, cmap='gray', vmin=0, vmax=1)
    _ = ax1.hist(im.ravel(), bins=100)
    fig.suptitle(title, fontsize=12)


def downscale(data):
    downscaling_func = {
        'median': np.nanmedian,
        'mean': np.nanmean
    }[config['downscaling_func']]

    block_sz = (config['downscaling_factor'], config['downscaling_factor'], 1)
    return block_reduce(data, block_size=block_sz, func=downscaling_func)


# https://pixinsight.com/forum/index.php?threads/auto-histogram-settings-to-replicate-auto-stf.8205/#post-55143
# donde (sea m=bg_val):
#   mtf(0, m, r) = 0
#   mtf(m, m, r) = bg_target_val
#   mtf(1, m, r) = 1
def mtf(x, bg_val, bg_target_val=0.5):
    m, r = bg_val, 1/bg_target_val
    return ( (1-m)*x ) / ( (1-r*m)*x + (r-1)*m ) 


def imtf(y, bg_val, bg_target_val=0.5):
    m, r = bg_val, 1/bg_target_val
    return mtf(y, 1-m, (r-1)/r)


# Mirroring np.nan_to_num
def num_to_nan(data, num=0.0):
    data[data == num] = np.nan


def delinearize(data, bg_target_val=0.25):
    # Subtract pedestal
    pedestal = np.nanmin(data)
    data -= pedestal
    
    # Scale to [0,1] range, mapping the given quantile (instead of the max) to 1.0.
    # This blows out the highlights, but we are interested in the background!
    scale = np.nanquantile(data, q=config['delinearization_quantile']) 
    data /= scale
    data = data.clip(0.0, 1.0)

    # Estimate background value
    bg_val = np.nanmedian(data)

    return mtf(data, bg_val, bg_target_val), pedestal, scale, bg_val


def linearize(data, pedestal, scale, bg_val, bg_target_val=0.25):
    data = imtf(data, bg_val, bg_target_val)
    data *= scale
    data += pedestal
    return data



# %%

# __/ Custom loss \__________
# TODO: meter esta función en la capa spline ??
def bg_loss_alpha(y_true, y_pred, model, alpha):
    # Residuals
    r = y_true - y_pred

    # Apply mask of residuals
    spline_layer = model.layers[1]
    if spline_layer.mask is not None:
        r *= spline_layer.mask

    abs_r = tf.math.abs(r)

    # TODO: mae abs_r vs mse r*r ...
    # tf.math.log(1+r*r)
    # 2*tf.math.reciprocal( 1+tf.math.exp(-15*r*r))-1 # tipo sigmoide
    #   https://www.wolframalpha.com/input/?i=Plot%5B2%2F%281%2Be%5E%28-15*x%5E2%29%29-1%2C+x+%3D+-1+to+1%5D
    error = tf.math.reduce_mean( abs_r , axis=-1) 

    # TODO: nombrar estos penalties
    penalty = tf.math.reduce_mean(abs_r - r, axis=-1)
    negative_bg = tf.math.reduce_mean(tf.math.abs(y_pred) - y_pred) # Éste aplica independientemente de la máscara

    #return error + alpha*penalty + 0.1*tf.math.reduce_mean(tf.math.square(spline_layer.ww)) #+ tf.math.reduce_mean(tf.math.square(spline_layer.vw))
    return error + alpha*(penalty + negative_bg) #+ 0.1*tf.math.reduce_mean(tf.math.square(spline_layer.ww)) #+ tf.math.reduce_mean(tf.math.square(spline_layer.vw))
    #return tf.math.log(0.001 + error + alpha*(penalty + negative_bg))


# __/ Callbacks \__________
earlystop = tf.keras.callbacks.EarlyStopping(
    monitor='loss', 
    min_delta=0.00005, 
    patience=100,
    restore_best_weights=True,
    verbose=True
)

reduce_lr = tf.keras.callbacks.ReduceLROnPlateau(
    monitor='loss', 
    factor=0.5,
    patience=50, 
    min_lr=0.0001,
    verbose=True
)

def lr_sched(epoch, lr):
    if epoch == 25:
        return 0.1 * lr
    else:
        return lr

lrsched = tf.keras.callbacks.LearningRateScheduler(lr_sched, verbose=True)


class PredictionCallback(tf.keras.callbacks.Callback):    
  def on_epoch_end(self, epoch, logs={}):
    y_pred = self.model.predict_on_batch(X)
    final = y_true[0,...,0] - y_pred[0,...,0]
    final -= final.min()
    final /= final.max()
    im = Image.fromarray( (255 * final).astype(np.uint8) )
    im.save("out\\Epoch_%03d.png" % (epoch,))

prediction = PredictionCallback()

callbacks = [earlystop, reduce_lr] #, prediction] #, lrsched]


# __/ Model fit and predict (spline fitting) \__________
def fit_spline(im, config):
    N, O, alpha, threshold = config['N'], config['O'],  config['alpha'], config['threshold']
    B, lr, epochs = config['B'], config['lr'], config['epochs']

    im_orig = np.expand_dims(im, axis=0)
    y_true = im_orig.repeat(B, axis=0)
    X = np.zeros(B) 
    #print(im_orig.shape, y_true.shape, X.shape)

    x = keras.layers.Input(shape=(), name='input_layer', batch_size=B)
    y = Spline(im, mask=threshold, n_control_points=N, order=O)(x)
    model = keras.Model(inputs=x, outputs=y, name="bgmodel")

    plot_train_points(model, im)
    plt.show()
    model.summary()

    optimizer = tf.keras.optimizers.Adam(learning_rate=lr)
    bg_loss = lambda y_true, y_pred: bg_loss_alpha(y_true, y_pred, model, alpha) 
    model.compile(optimizer, loss=bg_loss)

    history = model.fit(
        x=X, y=y_true, 
        epochs=epochs, 
        callbacks=callbacks
    )

    y_pred = model.predict(X)

    return y_pred, model, history


# __/ Draw spline train points \__________
def plot_train_points(model, im):
    h, w, _ = im.shape
    spline_layer = model.layers[1]
    train_points = spline_layer.train_points.numpy()[0]
    if spline_layer.mask is not None:
        x = im * spline_layer.mask
    else:
        x = im

    fig, ax = plt.subplots(figsize=(16,8))
    ax.imshow(x, cmap='gray')
    ax.plot(train_points[:,1]*w, train_points[:,0]*h, 'go', fillstyle='none')


# %%

# __/ Main script \__________

# Open original image
xisf = XISF(in_filepath)
im_orig = xisf.read_image(0)
_, im_orig_median, _ = statistics(im_orig, "Original")

# Preprocessing uses a copy
im = im_orig.copy()

# Ignore zero values (real data has some pedestal) by converting to NaN
num_to_nan(im)

# Delinearize to stretch the background
im, pedestal, scale, bg_val = delinearize(im)
_, im_median, _ = statistics(im, "Delinearized")

# Downscale
im = downscale(im)
_ = statistics(im, "Downscaled")

# Replace NaNs
np.nan_to_num(im, copy=False)

plot_image_hist(im, "Delinearized & downscaled")


# %%
# Fit spline
y_pred, model, history = fit_spline(im, config)
print(f"N, B, epochs, loss: {config['N']}, {config['B']}, {len(history.history['loss'])}, {min(history.history['loss']):.5f}")
plt.plot(history.history['loss'], label='Loss')


#%%
# Visualize mask
spline_layer = model.layers[1]
if spline_layer.mask is not None:
    plot_image_hist(spline_layer.mask.numpy(), "Mask")


# %%
# Visualize fitted spline (background model)
bg_hat = y_pred[0,...]
_ = statistics(bg_hat, "Bg model")
plot_image_hist(bg_hat, "Background model")


# %%
# Visualize final train points over the (masked) image
plot_train_points(model, im)


# %%
plot_image_hist(im-bg_hat+im_median, "Bg subtracted (downsized, delinearized)")


# %%
#final = im - bg
#plt.figure(figsize=(16,10))
#plt.imshow(final, cmap='gray')
#print("Range: ", final.min(), final.max())


# %%
#plt.imshow(-final.clip(-1,0), cmap='gray')
# TODO: salen valores negativos, es necesario hacer final -= final.min() para ajustar el 0. 

# %%
#final -= final.min()
#if final.max() > 1:
#    final /= final.max()

# %%
# Generate the final background model by interpolating the trained spline to the original image size
bg_fullres = spline_layer.interpolate(im_orig.shape, chunks=config['downscaling_factor']**2)
_ = statistics(bg_fullres, "Bg (full size)")
plot_image_hist(bg_fullres, "Background model (full size)")


# %%
# Stretched final image
#final_fullres = delinearize(im_orig)[0]
#np.nan_to_num(final_fullres, copy=False)
#final_fullres -= bg_fullres
#final_fullres -= final_fullres.min()
#if final_fullres.max() > 1:
#    final_fullres /= final.max()
#plt.imshow(final_fullres, vmin=0, vmax=1)


# %%
# Linearize the background model...
bg_fullres_linear = linearize(bg_fullres, pedestal, scale, bg_val)
_ = statistics(bg_fullres_linear, "Bg (linear)")

# ... and subtract it from the original image
im_final = im_orig - bg_fullres_linear
im_final_min, _, _ = statistics(im_final, "Subtracted")

# Visualize out of range (negative, really) values
plt.figure(figsize=(16,10))
plt.imshow(-im_final.clip(-1,0), cmap='gray')
plt.title("Pixels with negative value after subtraction")

# Apply pedestal so the final image has the same median value as the original
im_final -= im_final_min
im_final += im_orig_median
if im_final.max() > 1.0:
    im_final /= im_final.max()

_ = statistics(im_final, "Final")

# %%
plt.figure(figsize=(16,10))
plt.imshow(delinearize(im_final.copy(), 0.25)[0], cmap='gray')

# %%
os.makedirs(os.path.dirname(out_filepath), exist_ok=True)

# Write final image and background model to file
XISF.write(out_filepath, im_final, xisf.get_images_metadata()[0], xisf.get_file_metadata())
XISF.write(bg_filepath, bg_fullres_linear)

# %%
# Experiment: variance 
#experiment = []
#for _ in range(0, 15):
#    y_pred, model, history = fit_spline(im_orig, config)
#    bg = y_pred[0,...]
#    final = im_orig - bg#
#
#    data = {
#        'loss': min(history.history['loss']),
#        'epochs': len(history.history['loss']),
#        'min': final.min()
#    }
#    experiment.append(data)#
#
#df = pd.DataFrame(experiment)
#df[['loss']].plot()
#df.to_csv("%s_B%d_var.csv" % (filename, config['B']))


# %%
# Experiment: varying N
#experiment = []
#for N in [15, 25, 50, 75, 100, 125, 150, 175, 200, 225, 250, 275, 300]:
#    config['N'] = N
#    y_pred, model, history = fit_spline(im_orig, config)
#    bg = y_pred[0,...]
#    final = im_orig - bg
#
#    data = {
#        'N': N,
#        'loss': min(history.history['loss']),
#        'epochs': len(history.history['loss']),
#        'min': final.min()
#    }
#    experiment.append(data)
#
#df = pd.DataFrame(experiment).set_index("N")
#df[['loss']].plot()
#df.to_csv("%s_N.csv" % (filename,))
