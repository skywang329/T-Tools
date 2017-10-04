# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""ResNet model.

Related papers:
https://arxiv.org/pdf/1603.05027v2.pdf
https://arxiv.org/pdf/1512.03385v1.pdf
https://arxiv.org/pdf/1605.07146v1.pdf
"""

import numpy as np
import tensorflow as tf
import six

from tensorflow.python.training import moving_averages
from ttools.models.classifier import Classifier

class ResNet(Classifier):
    """ResNet model."""

    def __init__(self, batch_size=128,
                 num_classes=10,
                 min_lrn_rate=0.0001,
                 lrn_rate=0.1,
                 num_residual_units=5,
                 use_bottleneck=False,
                 weight_decay_rate=0.0002,
                 relu_leakiness=0.1,
                 optimizer='mom',
                 **kwargs):
        """ResNet constructor.

        Args:
          hps: Hyperparameters.
          images: Batches of images. [batch_size, image_size, image_size, 3]
          labels: Batches of labels. [batch_size, num_classes]
          mode: One of 'train' and 'eval'.
        """

        super().__init__(batch_size=batch_size,
                         num_classes=num_classes,
                         min_lrn_rate=min_lrn_rate,
                         lrn_rate=lrn_rate,
                         num_residual_units=num_residual_units,
                         use_bottleneck=use_bottleneck,
                         weight_decay_rate=weight_decay_rate,
                         relu_leakiness=relu_leakiness,
                         optimizer=optimizer,
                         **kwargs)

        self._extra_train_ops = []

    def _stride_arr(self, stride):
        """Map a stride scalar to the stride array for tf.nn.conv2d."""
        return [1, stride, stride, 1]

    def _build_model(self):
        """Build the core model within the graph."""
        with tf.variable_scope('init'):
            x = self.input_data
            x = self._conv('init_conv', x, 3, 3, 16, self._stride_arr(1))

        strides = [1, 2, 2]
        activate_before_residual = [True, False, False]
        if self.hps['use_bottleneck']:
            res_func = self._bottleneck_residual
            filters = [16, 64, 128, 256]
        else:
            res_func = self._residual
            filters = [16, 16, 32, 64]
            # Uncomment the following codes to use w28-10 wide residual network.
            # It is more memory efficient than very deep residual network and has
            # comparably good performance.
            # https://arxiv.org/pdf/1605.07146v1.pdf
            # filters = [16, 160, 320, 640]
            # Update hps.num_residual_units to 4

        with tf.variable_scope('unit_1_0'):
            x = res_func(x, filters[0], filters[1], self._stride_arr(strides[0]),
                         activate_before_residual[0])
        for i in six.moves.range(1, self.hps['num_residual_units']):
            with tf.variable_scope('unit_1_%d' % i):
                x = res_func(x, filters[1], filters[1], self._stride_arr(1), False)

        with tf.variable_scope('unit_2_0'):
            x = res_func(x, filters[1], filters[2], self._stride_arr(strides[1]),
                         activate_before_residual[1])
        for i in six.moves.range(1, self.hps['num_residual_units']):
            with tf.variable_scope('unit_2_%d' % i):
                x = res_func(x, filters[2], filters[2], self._stride_arr(1), False)

        with tf.variable_scope('unit_3_0'):
            x = res_func(x, filters[2], filters[3], self._stride_arr(strides[2]),
                         activate_before_residual[2])
        for i in six.moves.range(1, self.hps['num_residual_units']):
            with tf.variable_scope('unit_3_%d' % i):
                x = res_func(x, filters[3], filters[3], self._stride_arr(1), False)

        with tf.variable_scope('unit_last'):
            x = self._batch_norm('final_bn', x)
            x = self._relu(x, self.hps['relu_leakiness'])
            x = self._global_avg_pool(x)

        with tf.variable_scope('logit'):
            logits = self._fully_connected(x, self.hps['num_classes'])
            self.logits = logits
            self.predictions = tf.nn.softmax(logits)

        self.compute_cost(logits)

    def compute_cost(self, logits):
        with tf.variable_scope('costs'):
            xent = tf.nn.softmax_cross_entropy_with_logits(
                logits=logits, labels=self.input_labels)
            self.cost = tf.reduce_mean(xent, name='xent')
            self.cost += self._decay()

            tf.summary.scalar('cost', self.cost)

    def _build_train_op(self):
        """Build training specific ops for the graph."""
        self.lrn_rate = tf.constant(self.hps['lrn_rate'], tf.float32)
        tf.summary.scalar('learning_rate', self.lrn_rate)

        trainable_variables = tf.trainable_variables()
        grads = tf.gradients(self.cost, trainable_variables)

        if self.hps['optimizer'] == 'sgd':
            optimizer = tf.train.GradientDescentOptimizer(self.lrn_rate)
        elif self.hps['optimizer'] == 'mom':
            optimizer = tf.train.MomentumOptimizer(self.lrn_rate, 0.9)

        apply_op = optimizer.apply_gradients(
            zip(grads, trainable_variables),
            global_step=self.global_step, name='train_step')

        train_ops = [apply_op] + self._extra_train_ops
        self.train_op = tf.group(*train_ops)

    # TODO(xpan): Consider batch_norm in contrib/layers/python/layers/layers.py
    def _batch_norm(self, name, x):
        """Batch normalization."""
        with tf.variable_scope(name):
            params_shape = [x.get_shape()[-1]]

            beta = tf.get_variable(
                'beta', params_shape, tf.float32,
                initializer=tf.constant_initializer(0.0, tf.float32))
            gamma = tf.get_variable(
                'gamma', params_shape, tf.float32,
                initializer=tf.constant_initializer(1.0, tf.float32))

            if self.mode == 'train':
                mean, variance = tf.nn.moments(x, [0, 1, 2], name='moments')

                moving_mean = tf.get_variable(
                    'moving_mean', params_shape, tf.float32,
                    initializer=tf.constant_initializer(0.0, tf.float32),
                    trainable=False)
                moving_variance = tf.get_variable(
                    'moving_variance', params_shape, tf.float32,
                    initializer=tf.constant_initializer(1.0, tf.float32),
                    trainable=False)

                self._extra_train_ops.append(moving_averages.assign_moving_average(
                    moving_mean, mean, 0.9))
                self._extra_train_ops.append(moving_averages.assign_moving_average(
                    moving_variance, variance, 0.9))
            else:
                mean = tf.get_variable(
                    'moving_mean', params_shape, tf.float32,
                    initializer=tf.constant_initializer(0.0, tf.float32),
                    trainable=False)
                variance = tf.get_variable(
                    'moving_variance', params_shape, tf.float32,
                    initializer=tf.constant_initializer(1.0, tf.float32),
                    trainable=False)
                tf.summary.histogram(mean.op.name, mean)
                tf.summary.histogram(variance.op.name, variance)
            # epsilon used to be 1e-5. Maybe 0.001 solves NaN problem in deeper net.
            y = tf.nn.batch_normalization(
                x, mean, variance, beta, gamma, 0.001)
            y.set_shape(x.get_shape())
            return y

    def _residual(self, x, in_filter, out_filter, stride,
                  activate_before_residual=False):
        """Residual unit with 2 sub layers."""
        if activate_before_residual:
            with tf.variable_scope('shared_activation'):
                x = self._batch_norm('init_bn', x)
                x = self._relu(x, self.hps['relu_leakiness'])
                orig_x = x
        else:
            with tf.variable_scope('residual_only_activation'):
                orig_x = x
                x = self._batch_norm('init_bn', x)
                x = self._relu(x, self.hps['relu_leakiness'])

        with tf.variable_scope('sub1'):
            x = self._conv('conv1', x, 3, in_filter, out_filter, stride)

        with tf.variable_scope('sub2'):
            x = self._batch_norm('bn2', x)
            x = self._relu(x, self.hps['relu_leakiness'])
            x = self._conv('conv2', x, 3, out_filter, out_filter, [1, 1, 1, 1])

        with tf.variable_scope('sub_add'):
            if in_filter != out_filter:
                orig_x = tf.nn.avg_pool(orig_x, stride, stride, 'VALID')
                orig_x = tf.pad(
                    orig_x, [[0, 0], [0, 0], [0, 0],
                             [(out_filter - in_filter) // 2, (out_filter - in_filter) // 2]])
            x += orig_x

        tf.logging.debug('image after unit %s', x.get_shape())
        return x

    def _bottleneck_residual(self, x, in_filter, out_filter, stride,
                             activate_before_residual=False):
        """Bottleneck residual unit with 3 sub layers."""
        if activate_before_residual:
            with tf.variable_scope('common_bn_relu'):
                x = self._batch_norm('init_bn', x)
                x = self._relu(x, self.hps['relu_leakiness'])
                orig_x = x
        else:
            with tf.variable_scope('residual_bn_relu'):
                orig_x = x
                x = self._batch_norm('init_bn', x)
                x = self._relu(x, self.hps['relu_leakiness'])

        with tf.variable_scope('sub1'):
            x = self._conv('conv1', x, 1, in_filter, out_filter / 4, stride)

        with tf.variable_scope('sub2'):
            x = self._batch_norm('bn2', x)
            x = self._relu(x, self.hps['relu_leakiness'])
            x = self._conv('conv2', x, 3, out_filter / 4, out_filter / 4, [1, 1, 1, 1])

        with tf.variable_scope('sub3'):
            x = self._batch_norm('bn3', x)
            x = self._relu(x, self.hps['relu_leakiness'])
            x = self._conv('conv3', x, 1, out_filter / 4, out_filter, [1, 1, 1, 1])

        with tf.variable_scope('sub_add'):
            if in_filter != out_filter:
                orig_x = self._conv('project', orig_x, 1, in_filter, out_filter, stride)
            x += orig_x

        tf.logging.info('image after unit %s', x.get_shape())
        return x

    def _decay(self):
        """L2 weight decay loss."""
        costs = []
        for var in tf.trainable_variables():
            if var.op.name.find(r'DW') > 0:
                costs.append(tf.nn.l2_loss(var))
                # tf.summary.histogram(var.op.name, var)

        return tf.multiply(self.hps['weight_decay_rate'], tf.add_n(costs))

    def _conv(self, name, x, filter_size, in_filters, out_filters, strides):
        """Convolution."""
        with tf.variable_scope(name):
            n = filter_size * filter_size * out_filters
            kernel = tf.get_variable(
                'DW', [filter_size, filter_size, in_filters, out_filters],
                tf.float32, initializer=tf.random_normal_initializer(
                    stddev=np.sqrt(2.0 / n)))
            return tf.nn.conv2d(x, kernel, strides, padding='SAME')

    def _relu(self, x, leakiness=0.0):
        """Relu, with optional leaky support."""
        return tf.where(tf.less(x, 0.0), leakiness * x, x, name='leaky_relu')

    def _fully_connected(self, x, out_dim):
        """FullyConnected layer for final output."""
        x = tf.reshape(x, [self.hps['batch_size'], -1])
        w = tf.get_variable(
            'DW', [x.get_shape()[1], out_dim],
            initializer=tf.uniform_unit_scaling_initializer(factor=1.0))
        b = tf.get_variable('biases', [out_dim],
                            initializer=tf.constant_initializer())
        return tf.nn.xw_plus_b(x, w, b)

    def _global_avg_pool(self, x):
        assert x.get_shape().ndims == 4
        return tf.reduce_mean(x, [1, 2])

    def _build_hooks(self, *args):
        class _LearningRateSetterHook(tf.train.SessionRunHook):
            """Sets learning_rate based on global step."""

            def __init__(self, global_step, lrn_rate):
                tf.train.SessionRunHook.__init__(self)
                self.lrn_rate = lrn_rate
                self.global_step = global_step

            def begin(self):
                self._lrn_rate = 0.1

            def before_run(self, run_context):
                return tf.train.SessionRunArgs(
                    self.global_step,  # Asks for global step value.
                    feed_dict={self.lrn_rate: self._lrn_rate})  # Sets learning rate

            def after_run(self, run_context, run_values):
                train_step = run_values.results
                if train_step < 40000:
                    self._lrn_rate = 0.1
                elif train_step < 60000:
                    self._lrn_rate = 0.01
                elif train_step < 80000:
                    self._lrn_rate = 0.001
                else:
                    self._lrn_rate = 0.0001

        learning_rate_hook = _LearningRateSetterHook(self.global_step,
                                                     self.lrn_rate)
        super()._build_hooks(*args)
        self.hooks.append(learning_rate_hook)

    def predict(self, images, targets):
        self.mode = 'eval'
        self.input_data = images
        self.input_labels = targets
        self.build_graph()
        #TODO: Init from checkpoint
        curr_vars = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES,
                scope='inference')
        prev_vars = [var_name for var_name, _ in tf.contrib.framework.list_variables(self.model_path)]
        restore_map = {var.op.name.replace('inference/',''): var for var in curr_vars 
                if var.op.name.replace('inference/','') in prev_vars}
        tf.contrib.framework.init_from_checkpoint(self.model_path, restore_map)
        
        return self.predictions, self.logits

    def evaluate(self, input_data, targets):
        """Eval loop."""
        self.mode = 'eval'
        self.input_data = input_data
        self.input_labels = targets
        self.build_graph()
        curr_vars = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES,
                scope=self.name())
        prev_vars = [var_name for var_name, _ in tf.contrib.framework.list_variables(self.model_path)]
        restore_map = {var.op.name: var for var in curr_vars 
                if var.op.name in prev_vars}
        tf.contrib.framework.init_from_checkpoint(self.model_path, restore_map)
        init_op = tf.group(tf.global_variables_initializer(),
                tf.local_variables_initializer())


        summary_writer = tf.summary.FileWriter(self.model_path + '/eval')
        
        sess = tf.Session(config=tf.ConfigProto(allow_soft_placement=True))
        sess.run(init_op)
        saver = tf.train.Saver()
        tf.train.start_queue_runners(sess)

        best_precision = 0.0
        while True:
            try:
                ckpt_state = tf.train.get_checkpoint_state(self.model_path)
            except tf.errors.OutOfRangeError as e:
                tf.logging.error('Cannot restore checkpoint: %s', e)
                continue
            if not (ckpt_state and ckpt_state.model_checkpoint_path):
                tf.logging.info('No model to eval yet at %s', self.model_path)
                continue
            tf.logging.info('Loading checkpoint %s', ckpt_state.model_checkpoint_path)
            saver.restore(sess, ckpt_state.model_checkpoint_path)

            total_prediction, correct_prediction = 0, 0
            for _ in six.moves.range(50):
                (summaries, loss, predictions, truth, train_step) = sess.run(
                    [self.summaries, self.cost, self.predictions,
                     self.input_labels, self.global_step])

                truth = np.argmax(truth, axis=1)
                predictions = np.argmax(predictions, axis=1)
                correct_prediction += np.sum(truth == predictions)
                total_prediction += predictions.shape[0]

            precision = 1.0 * correct_prediction / total_prediction
            best_precision = max(precision, best_precision)

            precision_summ = tf.Summary()
            precision_summ.value.add(
                tag='Precision', simple_value=precision)
            summary_writer.add_summary(precision_summ, train_step)
            best_precision_summ = tf.Summary()
            best_precision_summ.value.add(
                tag='Best Precision', simple_value=best_precision)
            summary_writer.add_summary(best_precision_summ, train_step)
            summary_writer.add_summary(summaries, train_step)
            tf.logging.info('loss: %.3f, precision: %.3f, best precision: %.3f' %
                            (loss, precision, best_precision))
            summary_writer.flush()

            return
