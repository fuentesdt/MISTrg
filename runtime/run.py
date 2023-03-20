import os
import gc
import json
import pdb

import ants
import ctypes
import pandas as pd
import numpy as np
from tqdm import tqdm
from sklearn.model_selection import train_test_split
from sklearn.model_selection import KFold

import tensorflow as tf
from tensorflow.keras.models import load_model
import tensorflow.keras.backend as K

from models.get_model import get_model
from runtime.loss import DiceLoss, get_loss
from runtime.checkpoints import Checkpoints
from runtime.progress_bar import ProgressBar
from runtime.logger import Logger
from inference.main_inference import predict_single_example, load_test_time_models, test_time_inference
from inference.sliding_window import sliding_window_inference
from postprocess_preds.postprocess import Postprocess
from runtime.utils import get_files_df, get_lr_schedule, get_optimizer, get_flip_axes, \
    evaluate_prediction, compute_results_stats, init_results_df, set_seed, set_tf_flags, set_visible_devices, \
    set_memory_growth, set_amp, set_xla, get_test_df
from data_loading.dali_loader import get_data_loader
from kFoldMetrics import kFoldMetrics

class RunTime:

    def __init__(self, args):
        # Read user defined parameters
        self.args = args
        self.k_metrics = None
        with open(self.args.data, 'r') as file:
            self.data = json.load(file)

        if self.args.config is None:
            self.config_file = os.path.join(self.args.results, 'config.json')
        else:
            self.config_file = self.args.config

        with open(self.config_file, 'r') as file:
            self.config = json.load(file)

        self.n_channels = len(self.data['images'])
        self.n_classes = len(self.data['labels'])

        self.flip_axes = get_flip_axes()
        self.dice_loss = DiceLoss()

        # Get paths to dataset
        if self.args.paths is None:
            self.df = pd.read_csv(os.path.join(self.args.results, 'train_paths.csv'))
        else:
            self.df = pd.read_csv(self.args.paths)

        self.results_df = init_results_df(self.data)

    def predict_and_evaluate_val(self, model_path, df, ds):
        model = load_model(model_path, compile=False)
        num_patients = len(df)
        pbar = tqdm(total=num_patients)
        pred_temp_filename = os.path.join(self.args.results, 'predictions', 'train', 'raw', 'pred_temp.nii.gz')
        mask_temp_filename = os.path.join(self.args.results, 'predictions', 'train', 'raw', 'mask_temp.nii.gz')
        print("Saving run predictions to:", os.path.join(self.args.results, 'predictions', 'train', 'raw'))
        for step, (image, _) in enumerate(ds.take(len(df))):
            patient = df.iloc[step].to_dict()
            image_list = list(patient.values())[2:len(patient)]
            original_image = ants.image_read(image_list[0])

            prediction = predict_single_example(image,
                                                original_image,
                                                self.config,
                                                [model],
                                                self.args.sw_overlap,
                                                self.args.blend_mode,
                                                self.args.tta)

            prediction_filename = '{}.nii.gz'.format(patient['id'])
            ants.image_write(prediction,
                             os.path.join(self.args.results, 'predictions', 'train', 'raw', prediction_filename))

            # Evaluate prediction
            original_mask = ants.image_read(patient['mask'])
            eval_results = evaluate_prediction(prediction,
                                               original_mask,
                                               patient['id'],
                                               self.data,
                                               pred_temp_filename,
                                               mask_temp_filename,
                                               self.results_df.columns)

            # Update results df
            self.results_df = self.results_df.append(eval_results, ignore_index=True)

            # gc.collect()
            pbar.update(1)

        # Delete temporary files
        pbar.close()
        os.remove(pred_temp_filename)
        os.remove(mask_temp_filename)

        K.clear_session()
        del model
        gc.collect()

    def train(self):
        # Get folds for k-fold cross validation
        kfold = KFold(n_splits=self.args.nfolds, shuffle=True, random_state=42)

        images_dir = os.path.join(self.args.processed_data, 'images')
        images = [os.path.join(images_dir, file) for file in os.listdir(images_dir)]

        labels_dir = os.path.join(self.args.processed_data, 'labels')
        labels = [os.path.join(labels_dir, file) for file in os.listdir(labels_dir)]

        splits = kfold.split(list(range(len(images))))

        # Extract folds so that users can specify folds to train on
        train_splits = list()
        test_splits = list()
        for split in splits:
            train_splits.append(split[0])
            test_splits.append(split[1])

        # Get patch size if not specified by user
        if self.args.patch_size is None:
            patch_size = [64, 64, 64]
        else:
            patch_size = self.args.patch_size

        # Get network depth based on patch size
        if self.args.depth is None:
            depth = np.min([int(np.log(np.min(patch_size) / 4) / np.log(2)), 5])
        else:
            depth = self.args.depth

        self.config['patch_size'] = patch_size
        with open(self.config_file, 'w') as outfile:
            json.dump(self.config, outfile, indent=2)

        params = {}
        params['data'] = self.data
        params['tta'] = self.args.tta
        params['n_classes'] = self.n_classes
        params['config_labels'] = self.config['labels'] #(0, 1]
        params['sw_overlap'] = 0.5
        params['blend_mode'] = "gaussian"
        params['learning_rate'] = 0.001
        params['labels'] = labels
        params['patch_size'] = patch_size
        params['loss'] = True
        params['use_nz_mask'] = False
        params['target_spacing'] = self.config['target_spacing']
        params['min_component_size'] = 0
        params['prediction_dir'] = os.path.join(self.args.results, 'prediction')
        params['final_classes'] = self.config['labels']
        params['best_model_name'] = os.path.join(self.args.results, 'models', 
                                        '{}_best_model_split'.format(self.data['task']))
           
        params['current_model_name'] =  os.path.join(self.args.results, 'models', 'last',
                                           '{}_last_model_split'.format(self.data['task']))
        params['base_model_name'] = os.path.join(self.args.results, 'models', 'last',
                                           '{}_base_model_split'.format(self.data['task']))
        params['results_path'] = self.args.results

        self.k_metrics = kFoldMetrics(params)

        # Start training loop
        for fold in self.args.folds:
            print('Starting fold {}...'.format(fold))
            train_images = [images[idx] for idx in train_splits[fold]]
            train_labels = [labels[idx] for idx in train_splits[fold]]

            # Get validation set from training split
            train_images, val_images, train_labels, val_labels = train_test_split(train_images,
                                                                                  train_labels,
                                                                                  test_size=0.1,
                                                                                  random_state=self.args.seed)

            # Get DALI loaders
            train_loader = get_data_loader(imgs=train_images,
                                           lbls=train_labels,
                                           batch_size=self.args.batch_size,
                                           mode='train',
                                           seed=self.args.seed,
                                           num_workers=8,
                                           oversampling=self.args.oversampling,
                                           patch_size=patch_size)

            val_loader = get_data_loader(imgs=val_images,
                                         lbls=val_labels,
                                         batch_size=1,
                                         mode='eval',
                                         seed=self.args.seed,
                                         num_workers=8)

            if self.args.steps_per_epoch is None:
                self.args.steps_per_epoch = len(train_images) // self.args.batch_size
            else:
                self.args.steps_per_epoch = self.args.steps_per_epoch

            # Set up optimizer
            optimizer = get_optimizer(self.args)
           

            # Get loss function
            if self.args.use_precomputed_weights:
                class_weights = self.config['class_weights']
            else:
                class_weights = None

            loss_fn = get_loss(self.args, class_weights=class_weights)

            # Get model
            model = get_model(self.args.model,
                              input_shape=tuple(patch_size),
                              n_channels=len(self.data['images']),
                              n_classes=self.n_classes,
                              init_filters=self.args.init_filters,
                              depth=depth,
                              pocket=self.args.pocket,
                              config=self.config)

            train_loss = tf.keras.metrics.Mean('loss', dtype=tf.float32)

            @tf.function
            def train_step(image, mask):
                with tf.GradientTape() as tape:
                    pred = model(image)

                    unscaled_loss = loss_fn(mask, pred)
                    loss = unscaled_loss
                    if self.args.amp:
                        loss = optimizer.get_scaled_loss(unscaled_loss)

                gradients = tape.gradient(loss, model.trainable_variables)
                if self.args.amp:
                    gradients = optimizer.get_unscaled_gradients(gradients)
                optimizer.apply_gradients(zip(gradients, model.trainable_variables))

                train_loss(unscaled_loss)

            val_loss = tf.keras.metrics.Mean('val_loss', dtype=tf.float32)

            def val_step(image, mask):
                #print('shape',np.shape(mask), np.shape(image))
                #print('sw_overlap',self.args.sw_overlap )
                #print('blend_mode', self.args.blend_mode)
                #print('patchsize', patch_size)
                pred = sliding_window_inference(image,
                                                n_class=self.n_classes,
                                                roi_size=tuple(patch_size),
                                                sw_batch_size=1,
                                                overlap=self.args.sw_overlap,
                                                blend_mode=self.args.blend_mode,
                                                model=model)

                val_loss(self.dice_loss(mask, pred))
                # gc.collect()

            # Setup checkpoints for training
            best_val_loss = np.Inf
            best_model_path = os.path.join(self.args.results, 'models', 'best',
                                           '{}_best_model_split_{}'.format(self.data['task'], fold))
            last_model_path = os.path.join(self.args.results, 'models', 'last',
                                           '{}_last_model_split_{}'.format(self.data['task'], fold))

            checkpoint = Checkpoints(best_model_path, last_model_path, best_val_loss)

            # Setup progress bar
            progress_bar = ProgressBar(self.args.steps_per_epoch, len(val_images), train_loss, val_loss)

            # Setup logging
            logs = Logger(self.args, fold, train_loss, val_loss)

            total_steps = self.args.epochs * self.args.steps_per_epoch
            current_epoch = 1
            local_step = 1
            for global_step, (image, mask) in enumerate(train_loader):
                image = tf.image.rgb_to_grayscale(image)
                mask = tf.image.rgb_to_grayscale(mask)
                print("image size----->", image.get_shape())
                print("mask size----->", mask.get_shape())
                if global_step >= total_steps:
                    break

                if local_step == 1:
                    print('Fold {}: Epoch {}/{}'.format(fold, current_epoch, self.args.epochs))
                #x_in = tf.identity(image)
                #x_ = tf.placeholder(shape=[ shape[0], shape[1], shape[2], shape[3], dtype=tf.float32)
                #x_out = sess.run(x_in, feed_dict={x_: image[:,:,:,:]})

                train_step(image, mask)
                progress_bar.update_train_bar()
                local_step += 1

                if (global_step + 1) % self.args.steps_per_epoch == 0 and global_step > 0:
                    # Perform validation
                    #self.k_metrics.compute_val_loss(patch_size, model, val_loader, val_images, val_loss)

                    for _, (val_image, val_mask) in enumerate(val_loader.take(len(val_images))):
                        val_step(val_image, val_mask)
                        progress_bar.update_val_bar()
                        #self.k_metrics.compute_val_loss(patch_size, model, val_loader, val_images, val_loss, val_image, val_mask)
                    
                    current_val_loss = val_loss.result().numpy()
                    print('saving model', current_val_loss)
                    checkpoint.update(model, current_val_loss)
                    logs.update(current_epoch)

                    progress_bar.reset()
                    current_epoch += 1
                    local_step = 1
                    gc.collect()
            if self.args.optimizer == 'adam':
                learning_rate = optimizer.learning_rate.numpy() # _decayed_lr(tf.float32)
            # End of epoch training for fold
            self.k_metrics.on_epoch_end(patch_size, model, val_loader, val_images, val_loss, learning_rate)
            # Save last model
            print('Training for fold {} complete...'.format(fold))
            print('saving model', best_model_path)
            checkpoint.save_last_model(model, best_model_path + '/best/')
            
            # RG Moved 
            #K.clear_session()
            #del model, train_loader, val_loader
            #gc.collect()

            # Run prediction on test set and write results to .nii.gz format
            # Prepare test set
            test_images = [images[idx] for idx in test_splits[fold]]
            test_images.sort()

            test_labels = [labels[idx] for idx in test_splits[fold]]
            test_labels.sort()

            
            test_loader = get_data_loader(imgs=test_images,
                                          lbls=test_labels,
                                          batch_size=1,
                                          mode='eval',
                                          seed=42,
                                          num_workers=8)

            # Bug fix: Strange behavior with numerical ids
            test_df_ids = [pat.split('/')[-1].split('.')[0] for pat in test_images]
            test_df = get_test_df(self.df, test_df_ids)

            # print('Running inference on validation set...')
            self.predict_and_evaluate_val(best_model_path, test_df, test_loader)
            self.k_metrics.on_kFold_end( model, test_df, test_loader)
            
            
            K.clear_session()
            del model, train_loader, val_loader
            del test_loader
            gc.collect()

            # End of fold
        self.k_metrics.on_training_end()
        K.clear_session()
        gc.collect()
        # End train function

    def run(self):

        os.environ["TF_GPU_THREAD_MODE"] = "gpu_private"
        os.environ["TF_GPU_THREAD_COUNT"] = "1"

        # Set seed if specified
        if not (self.args.seed is None):
            set_seed(self.args.seed)

        # Set visible devices
        set_visible_devices(self.args)

        # Allow memory growth
        set_memory_growth()

        # Set AMP and XLA if called for
        if self.args.amp:
            set_amp()

        if self.args.xla:
            set_xla()

        # Initialize horovod
        #hvd_init()

        # Set tf flags
        set_tf_flags()

        # Run training pipeline
        self.train()

        # Get final statistics
        self.results_df = compute_results_stats(self.results_df)

        # Write results to csv file
        print("Saving run dice results to csv folder:", os.path.join(self.args.results, 'results_run.csv'))
        self.results_df.to_csv(os.path.join(self.args.results, 'results_run.csv'), index=False)

        # Run post-processing
        postprocess = Postprocess(self.args)
        postprocess.run()

        # Run inference on test set if it is provided
        if 'test-data' in self.data.keys():
            print('Running inference on test set...')
            test_df = get_files_df(self.data, 'test')
            models_dir = os.path.join(self.args.results, 'models', 'best')

            models = load_test_time_models(models_dir, False)

            test_time_inference(test_df,
                                os.path.join(self.args.results, 'predictions', 'test'),
                                self.config_file,
                                models,
                                self.args.sw_overlap,
                                self.args.blend_mode,
                                True)

        K.clear_session()
        gc.collect()
