#!/usr/bin/env python

# --------------------------------------------------------
# Fast R-CNN
# Copyright (c) 2015 Microsoft
# Licensed under The MIT License [see LICENSE for details]
# Written by Ross Girshick
# --------------------------------------------------------

"""
Train post-hoc SVMs using the algorithm and hyper-parameters from
traditional R-CNN.
"""
from __future__ import division
import _init_paths
from fast_rcnn.config import cfg, cfg_from_file
from datasets.factory import get_imdb
from fast_rcnn.test import im_detect
from utils.timer import Timer
import caffe
import argparse
import pprint
import numpy as np
import numpy.random as npr
import cv2
from sklearn import svm
from sklearn.calibration import _sigmoid_calibration
from sklearn.cross_validation import train_test_split
import cPickle as pickle
import os, sys
from easydict import EasyDict as edict

class SVMTrainer(object):
    """
    Trains post-hoc detection SVMs for all classes using the algorithm
    and hyper-parameters of traditional R-CNN.
    """

    def __init__(self, net, imdb,svm_cfg,fold_idx=None):
        self.cfg = svm_cfg
        self.imdb = imdb
        self.net = net
        self.layer = 'fc7'
        self.cls_prm = 'cls_score'
        dim = net.params[self.cls_prm][0].data.shape[1]
        scale = self.cfg.SCALE#self._get_feature_scale()
        self.fold_idx = fold_idx
        print('Feature dim: {}'.format(dim))
        print('Feature scale: {:.3f}'.format(scale))
        self.trainers = [SVMClassTrainer(cls, dim,self.cfg, feature_scale=scale)
                         for cls in imdb.classes]

    def _get_feature_scale(self, num_images=100):
        TARGET_NORM = 20.0 # Magic value from traditional R-CNN
        _t = Timer()
        roidb = self.imdb.roidb
        total_norm = 0.0
        count = 0.0
        inds = npr.choice(xrange(self.imdb.num_images), size=num_images,
                          replace=False)
        for i_, i in enumerate(inds):
            im = cv2.imread(self.imdb.image_path_at(i))
            if roidb[i]['flipped']:
                im = im[:, ::-1, :]
            _t.tic()
            scores, boxes = im_detect(self.net, im, roidb[i]['boxes'])
            _t.toc()
            feat = self.net.blobs[self.layer].data
            total_norm += np.sqrt((feat ** 2).sum(axis=1)).sum()
            count += feat.shape[0]
            print('{}/{}: avg feature norm: {:.3f}'.format(i_ + 1, num_images,
                                                           total_norm / count))

        return TARGET_NORM * 1.0 / (total_norm / count)

    def _get_pos_counts(self):
        counts = np.zeros((len(self.imdb.classes)), dtype=np.int)
        roidb = self.imdb.roidb
        for i in xrange(len(roidb)):
            for j in xrange(1, self.imdb.num_classes):
                I = np.where(roidb[i]['gt_classes'] == j)[0]
                counts[j] += len(I)

        for j in xrange(1, self.imdb.num_classes):
            print('class {:s} has {:d} positives'.
                  format(self.imdb.classes[j], counts[j]))

        return counts

    def gen_pos_examples(self):
        '''
        Collects positive examples from all images.
        stores them in a directory, one file for each class
        If folds are used, save along a fold index
        '''
        cache_dir = os.path.join(self.imdb.cache_path,'svm_pos')
        if os.path.exists(cache_dir): return
        #collect positive examples
        counts = self._get_pos_counts()
        for i in xrange(len(counts)):
            self.trainers[i].alloc_pos(counts[i])
        fold_idx = [[] for _ in xrange(self.imdb.num_classes)]
        _t = Timer()
        roidb = self.imdb.roidb
        num_images = len(roidb)
        for i in xrange(num_images):
            im = cv2.imread(self.imdb.image_path_at(i))
            if roidb[i]['flipped']:
                im = im[:, ::-1, :]
            gt_inds = np.where(roidb[i]['gt_classes'] > 0)[0]
            gt_boxes = roidb[i]['boxes'][gt_inds]
            _t.tic()
            scores, boxes = im_detect(self.net, im, gt_boxes)
            _t.toc()
            feat = self.net.blobs[self.layer].data
            for j in xrange(1, self.imdb.num_classes):
                cls_inds = np.where(roidb[i]['gt_classes'][gt_inds] == j)[0]
                if len(cls_inds) > 0:
                    cls_feat = feat[cls_inds, :]
                    self.trainers[j].append_pos(cls_feat)
                    if imdb.k_fold:
                        fold_idx[j] += [imdb.folds[j,i]] * len(cls_inds)
            print 'get_pos_examples: {:d}/{:d} {:.3f}s'.format(i + 1, len(roidb), _t.average_time)
        print 'saving pos examples to cache'
        os.makedirs(cache_dir)
        for j in xrange(1, self.imdb.num_classes):
            cache_file = os.path.join(cache_dir,  'pos_class_{:d}.pkl'.format(j))
            pickle.dump([self.trainers[j].pos,np.asarray(fold_idx[j])],open(cache_file,'wb'))
        print 'Done saving pos examples to cache'


    def get_class_pos_examples(self,class_idx):
        cache_dir = os.path.join(self.imdb.cache_path,'svm_pos')
        cache_file = os.path.join(cache_dir,  'pos_class_{:d}.pkl'.format(class_idx))
        self.trainers[class_idx].pos,fold_array = pickle.load(open(cache_file,'rb'))
        #if not flip - take only half
        #TODO - need to make sure pos examples are always generated with flip
        if not self.cfg.USE_FLIPPED:
            tmp_pos_num = self.trainers[class_idx].pos.shape[0]
            assert tmp_pos_num % 2==0,'Must be even number of positive examples'
            assert self.trainers[class_idx].pos.shape[0]==len(fold_array),'pos example and fold array dims are different'
            self.trainers[class_idx].pos = self.trainers[class_idx].pos[:tmp_pos_num/2,:]
            fold_array = fold_array[:tmp_pos_num/2]
        if self.fold_idx is not None:
            keep_idx = np.argwhere(fold_array!=self.fold_idx).ravel()
            self.trainers[class_idx].pos = self.trainers[class_idx].pos[keep_idx,:]
        self.trainers[class_idx].pos_cur = self.trainers[class_idx].pos.shape[0]

    '''
    #TODO-remove
    def get_pos_examples(self):
        #debug-add folds
        if self.fold_idx is not None:
            cache_file = os.path.join(self.imdb.cache_path, self.imdb.name + '_svm_pos_fold_{}.pkl'.format(self.fold_idx))
        else:
            cache_file = os.path.join(self.imdb.cache_path, self.imdb.name + '_svm_pos.pkl')
        if os.path.exists(cache_file):
            with open(cache_file, 'rb') as fid:
                pos_db = pickle.load(fid)
            for j in range(len(self.trainers)):
                self.trainers[j].pos = pos_db[j]
                self.trainers[j].pos_cur = pos_db[j].shape[0]
            print 'svm pos db loaded from {}'.format(cache_file)
            return                
        counts = self._get_pos_counts()
        for i in xrange(len(counts)):
            self.trainers[i].alloc_pos(counts[i])

        _t = Timer()
        roidb = self.imdb.roidb
        num_images = len(roidb)
        for i in xrange(num_images):
            im = cv2.imread(self.imdb.image_path_at(i))
            if roidb[i]['flipped']:
                im = im[:, ::-1, :]
            gt_inds = np.where(roidb[i]['gt_classes'] > 0)[0]
            gt_boxes = roidb[i]['boxes'][gt_inds]
            _t.tic()
            scores, boxes = im_detect(self.net, im, gt_boxes)
            _t.toc()
            #debug - save bounding boxes
            #self.imdb.debug_output_gt_roidb(im,gt_boxes,roidb[i]['gt_classes'][gt_inds],polarity='Pos')

            feat = self.net.blobs[self.layer].data
            for j in xrange(1, self.imdb.num_classes):
                #debug-add folds
                if imdb.k_fold and imdb.folds[j,i]==self.fold_idx:continue

                cls_inds = np.where(roidb[i]['gt_classes'][gt_inds] == j)[0]
                if len(cls_inds) > 0:
                    cls_feat = feat[cls_inds, :]
                    self.trainers[j].append_pos(cls_feat)

            print 'get_pos_examples: {:d}/{:d} {:.3f}s' \
                  .format(i + 1, len(roidb), _t.average_time)

        #saving positive examples to cache
        pos_db = [tr.pos for tr in self.trainers]
        with open(cache_file, 'wb') as fid:
            pickle.dump(pos_db, fid)
        print 'wrote svm pos db to {}'.format(cache_file)
    '''


    def initialize_net(self):
        print 'Initializing net'
        # Start all SVM parameters at zero
        self.net.params[self.cls_prm][0].data[...] = 0
        self.net.params[self.cls_prm][1].data[...] = 0

    def update_net(self, cls_ind, w, b):
        self.net.params[self.cls_prm][0].data[cls_ind, :] = w
        self.net.params[self.cls_prm][1].data[cls_ind] = b
        
    def train_with_hard_negatives(self):
        _t = Timer()
        roidb = self.imdb.roidb
        num_images = len(roidb)
        image_ind = [0] * self.imdb.num_classes
        for i in xrange(num_images):
            if min(image_ind)>=self.cfg.MAX_NUM_HARD_NEG_IMAGES: break
            im = cv2.imread(self.imdb.image_path_at(i))
            if roidb[i]['flipped']:
                im = im[:, ::-1, :]
            _t.tic()
            scores, boxes = im_detect(self.net, im, roidb[i]['boxes'])
            _t.toc()
            feat = self.net.blobs[self.layer].data
            for j in xrange(1, self.imdb.num_classes):
                #debug-add folds
                if image_ind[j]>=self.cfg.MAX_NUM_HARD_NEG_IMAGES:continue
                if imdb.k_fold and imdb.folds[j,i]==self.fold_idx:continue
                example_num = self.trainers[j].neg.shape[0] + self.trainers[j].pos.shape[0]
                ratio       = self.trainers[j].neg.shape[0] / self.trainers[j].pos.shape[0]
                if self.fold_idx is None:
                    print 'Start hard negative mining {:d}/{:d}, class {:d}/{:d}'.format(image_ind[j]+1,num_images,j+1,self.imdb.num_classes)
                else:
                    print 'Start hard negative mining fold {:d} {:d}/{:d}, class {:d}/{:d}'.format(self.fold_idx,image_ind[j]+1,num_images,j+1,self.imdb.num_classes)
                image_ind[j]+=1

                if self.cfg.MAX_NUM_HARD_NEG_EXAMPLES < example_num : continue
                hard_inds = \
                    np.where((scores[:, j] > self.cfg.HARD_THRESH) &
                             (roidb[i]['gt_overlaps'][:, j].toarray().ravel() <
                              self.cfg.NEG_IOU_THRESH))[0]
                if len(hard_inds) > 0:
                    #debug - save bounding boxes after the first image
                    #if i>0:
                    #    self.imdb.debug_output_gt_roidb(im,roidb[i]['boxes'][hard_inds,:],j * np.ones(len(hard_inds),dtype=np.int),polarity='Neg')
                    hard_feat = feat[hard_inds, :].copy()
                    new_w_b = \
                        self.trainers[j].append_neg_and_retrain(feat=hard_feat)
                    if new_w_b is not None:
                        self.update_net(j, new_w_b[0], new_w_b[1])


    def save_sigmoids(self,svm_models_file_name):
        params = [tr.sigmoid_params for tr in self.trainers]
        pickle.dump(params, open(svm_models_file_name, "wb"))

    def train(self):
        print 'Start dataset SVM training'       
        # Initialize SVMs using
        #   a. w_i = fc8_w_i - fc8_w_0
        #   b. b_i = fc8_b_i - fc8_b_0
        #   c. Install SVMs into net
        self.initialize_net()

        # Pass over roidb to count num positives for each class
        #   a. Pre-allocate arrays for positive feature vectors
        # Pass over roidb, computing features for positives only
        self.gen_pos_examples()

        #debug - stop here!! remove after generation of positives
        import pdb;pdb.set_trace()

        for j in xrange(1, self.imdb.num_classes):
            print 'loading positive examples for class {}'.format(j)
            self.get_class_pos_examples(j)

        # Pass over roidb
        #   a. Compute cls_score with forward pass
        #   b. For each class
        #       i. Select hard negatives
        #       ii. Add them to cache
        #   c. For each class
        #       i. If SVM retrain criteria met, update SVM
        #       ii. Install new SVM into net
        self.train_with_hard_negatives()

        # One final SVM retraining for each class
        # fit sigmoid to estimate probability
        # Install SVMs into net
        for j in xrange(1, self.imdb.num_classes):
            new_w_b = self.trainers[j].append_neg_and_retrain(force=True)
            self.update_net(j, new_w_b[0], new_w_b[1])
            sigmoid_neg = self.sample_random_neg(self.cfg.NUM_SAMPLES_SIGMOID_FIT-self.trainers[j].pos.shape[0],j)
            self.trainers[j].fit_probability_sigmoid(sigmoid_neg)
 

    def sample_random_neg(self,neg_num,j):
        neg = np.zeros((0,self.trainers[0].dim))
        for i in xrange(len(self.imdb.roidb)):
            im = cv2.imread(self.imdb.image_path_at(i))
            if self.imdb.roidb[i]['flipped']:im = im[:, ::-1, :]
            scores, boxes = im_detect(self.net, im, self.imdb.roidb[i]['boxes'])
            feat = self.net.blobs[self.layer].data
            neg_idx = np.where((self.imdb.roidb[i]['gt_overlaps'][:, j].toarray().ravel() < self.cfg.NEG_IOU_THRESH))[0]
            neg = np.vstack((neg, feat[neg_idx, :].copy()))     
            if neg.shape[0]>=neg_num: break
        return neg[:neg_num,:]
     
        

class SVMClassTrainer(object):
    """Manages post-hoc SVM training for a single object class."""

    def __init__(self, cls, dim, svm_cfg, feature_scale=1.0,
                 C=0.001, B=10.0, pos_weight=1.0):
        self.cfg = svm_cfg
        self.pos = np.zeros((0, dim), dtype=np.float32)
        self.neg = np.zeros((0, dim), dtype=np.float32)
        self.B = B
        self.C = self.cfg.C
        self.cls = cls
        self.pos_weight = self.cfg.BASE_POS_WEIGHT
        self.dim = dim
        self.feature_scale = feature_scale
        self.svm = svm.LinearSVC(C=self.cfg.C, class_weight={1: self.pos_weight, -1: 1},
                                 intercept_scaling=B, verbose=1,
                                 penalty='l2', loss='hinge',
                                 random_state=cfg.RNG_SEED, dual=True)

        self.pos_cur = 0
        self.num_neg_added = 0
        self.loss_history = []
        self.sigmoid_params = []

    def alloc_pos(self, count):
        self.pos_cur = 0
        self.pos = np.zeros((count, self.dim), dtype=np.float32)

    def append_pos(self, feat):
        num = feat.shape[0]
        #try to catch exeption
        if self.pos[self.pos_cur:self.pos_cur + num, :].shape!=feat.shape:
            import pdb; pdb.set_trace()

        self.pos[self.pos_cur:self.pos_cur + num, :] = feat
        self.pos_cur += num

    def train(self):
        print('>>> Updating {} detector <<<'.format(self.cls))
        num_pos = self.pos.shape[0]
        num_neg = self.neg.shape[0]
        print('Cache holds {} pos examples and {} neg examples'.format(num_pos, num_neg))
        X = np.vstack((self.pos, self.neg)) * self.feature_scale
        y = np.hstack((np.ones(num_pos),
                      -np.ones(num_neg)))
        if self.cfg.USE_INV_PROP_WEIGHTS:
            curr_pos_w = self.pos_weight * (num_neg / (num_pos + num_neg))
            curr_neg_w = 1.              * (num_pos / (num_pos + num_neg))
        else:
            curr_pos_w = self.pos_weight
            curr_neg_w = 1.
        self.svm.class_weight = {1: curr_pos_w, -1: curr_neg_w}
        print 'Liblinear start'
        self.svm.fit(X, y)
        print 'Liblinear end'
        w = self.svm.coef_
        b = self.svm.intercept_[0]
        scores = self.svm.decision_function(X)
        pos_scores = scores[:num_pos]
        neg_scores = scores[num_pos:]
        pos_loss = self.C * curr_pos_w * np.maximum(0, 1 - pos_scores).sum()
        neg_loss = self.C * curr_neg_w * np.maximum(0, 1 + neg_scores).sum()
        reg_loss = 0.5 * np.dot(w.ravel(), w.ravel()) + 0.5 * b ** 2
        tot_loss = pos_loss + neg_loss + reg_loss
        self.loss_history.append((tot_loss, pos_loss, neg_loss, reg_loss))

        for i, losses in enumerate(self.loss_history):
            print(('    {:d}: obj val: {:.3f} = {:.3f} '
                   '(pos) + {:.3f} (neg) + {:.3f} (reg)').format(i, *losses))
        return ((w * self.feature_scale, b),pos_scores, neg_scores)


    def append_neg_and_retrain(self, feat=None, force=False):
        if feat is not None:
            num = feat.shape[0]
            self.neg = np.vstack((self.neg, feat))
            self.num_neg_added += num
        if self.num_neg_added > self.cfg.RETRAIN_LIMIT or force:
            self.num_neg_added = 0
            new_w_b, pos_scores, neg_scores = self.train()
            not_easy_inds = np.where(neg_scores >= self.cfg.EVICT_THRESH)[0]
            if len(not_easy_inds) > 0:
                self.neg = self.neg[not_easy_inds, :]
            print('    Pruning easy negatives')
            print('    Cache holds {} pos examples and {} neg examples'.
                  format(self.pos.shape[0], self.neg.shape[0]))
            print('    {} pos support vectors'.format((pos_scores <= 1).sum()))
            print('    {} neg support vectors'.format((neg_scores >= -1).sum()))
            return new_w_b
        else:
            return None

    def fit_probability_sigmoid(self,neg):
        print('Fitting probablity sigmoid to {} detector'.format(self.cls))
        pos = self.pos
        num_pos = pos.shape[0]
        num_neg = neg.shape[0]
        X = np.vstack((pos, neg)) * self.feature_scale
        y = np.hstack((np.ones(num_pos),-np.ones(num_neg)))
        A,B = _sigmoid_calibration(self.svm.decision_function(X), y, sample_weight=None)
        self.sigmoid_params = [A,B]
        #self.evaluate_sigmoid_match(X_test,y_test,A,B)
        print('Sigmoid parameters : {},{}'.format(self.sigmoid_params[0],self.sigmoid_params[1]))


    def evaluate_sigmoid_match(self,X_test,y_test,A,B):
        from sklearn.calibration import calibration_curve
        import matplotlib.pyplot as plt
        from sklearn.metrics import (brier_score_loss, precision_score, recall_score,f1_score)
        prob_pos = 1. / (1. + (np.exp(A * X_test + B)))
        clf_score = brier_score_loss(y_test, prob_pos, pos_label=y_test.max())
        fraction_of_positives, mean_predicted_value = calibration_curve(y_test, prob_pos, n_bins=10)
        print("SVC_sigmoid:")
        print("\tBrier: %1.3f" % (clf_score))
        fig = plt.figure(2, figsize=(10, 10))
        ax1 = plt.subplot2grid((3, 1), (0, 0), rowspan=2)
        ax2 = plt.subplot2grid((3, 1), (2, 0))
        ax1.plot([0, 1], [0, 1], "k:", label="Perfectly calibrated")  
        ax1.plot(mean_predicted_value, fraction_of_positives, "s-",label="%s (%1.3f)" % ("SVC_sigmoid", clf_score))
        ax2.hist(prob_pos, range=(0, 1), bins=10, label="SVC_sigmoid",histtype="step", lw=2)
        ax1.set_ylabel("Fraction of positives")
        ax1.set_ylim([-0.05, 1.05])
        ax1.legend(loc="lower right")
        ax1.set_title('Calibration plots  (reliability curve)')
        ax2.set_xlabel("Mean predicted value")
        ax2.set_ylabel("Count")
        ax2.legend(loc="upper center", ncol=2)
        plt.tight_layout()
        plt.show()


def parse_args():
    """
    Parse input arguments
    """
    parser = argparse.ArgumentParser(description='Train SVMs (old skool)')
    parser.add_argument('--gpu', dest='gpu_id', help='GPU device id to use [0]',
                        default=0, type=int)
    parser.add_argument('--def', dest='prototxt',
                        help='prototxt file defining the network',
                        default=None, type=str)
    parser.add_argument('--net', dest='caffemodel',
                        help='model to test',
                        default=None, type=str)
    parser.add_argument('--cfg', dest='cfg_file',
                        help='optional config file', default=None, type=str)
    parser.add_argument('--imdb', dest='imdb_name',
                        help='dataset to train on',
                        default='disney_dataset', type=str)
    parser.add_argument('--set', dest='image_set',
                        help='image set name. train or test',
                        default='train', type=str)
    parser.add_argument('--outdir', dest='outdir',
                        help='Directory to write training results to',
                        default=None, type=str)
    parser.add_argument('--k_fold', dest='k_fold',
                        help='Optional k-fold support',
                        default=None, type=int)

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)

    args = parser.parse_args()
    return args

def save_svm_coeffs(net,file_name):
    print 'saving svm coeffs : {:s}'.format(file_name)
    w = net.params['cls_score'][0].data[...]
    b = net.params['cls_score'][1].data[...]
    pickle.dump([w,b],open(file_name, "wb"))

def get_svm_configuration():
    config = edict()
    config.USE_FLIPPED = True#False TODO-change back after generation of positives
    config.MAX_NUM_HARD_NEG_IMAGES = 100
    config.MAX_NUM_HARD_NEG_EXAMPLES = 20000
    config.USE_INV_PROP_WEIGHTS = False
    config.BASE_POS_WEIGHT = 2#1
    config.C = 0.001
    config.NUM_SAMPLES_SIGMOID_FIT = 10000
    config.HARD_THRESH = -1.0001
    config.NEG_IOU_THRESH = 0.3
    config.SCALE = 0.4
    config.RETRAIN_LIMIT = 2000
    config.EVICT_THRESH = -1.1
    return config

def writeDict(dict, filename):
    import datetime
    with open(filename, "a") as f:
        f.write(str(datetime.datetime.now()) + "\n")
        for i in dict.keys():            
            f.write(i + " : {}".format(dict[i]) + "\n")


if __name__ == '__main__':
    args = parse_args()
    if not os.path.exists(args.outdir):
        os.makedirs(args.outdir)
    print('Called with args:')
    print(args)
    svm_cfg = get_svm_configuration()
    #write configuration file to dir
    out_dir = args.outdir
    writeDict(svm_cfg, '{}/svm_cfg.txt'.format(out_dir))
    # Must turn this off to prevent issues when digging into the net blobs to
    # pull out features (tricky!)
    cfg.DEDUP_BOXES = 0
    # Must turn this on because we use the test im_detect() method to harvest
    # hard negatives
    cfg.TEST.SVM = True
    cfg.TRAIN.USE_FLIPPED = svm_cfg.USE_FLIPPED

    if args.cfg_file is not None:
        cfg_from_file(args.cfg_file)

    print('Using config:')
    pprint.pprint(cfg)

    # fix the random seed for reproducibility
    np.random.seed(cfg.RNG_SEED)

    # set up caffe
    caffe.set_mode_gpu()
    if args.gpu_id is not None:
        caffe.set_device(args.gpu_id)
    net = caffe.Net(args.prototxt, args.caffemodel, caffe.TEST)
    net.name = os.path.splitext(os.path.basename(args.caffemodel))[0]
    #load dataset
    imdb_name = args.imdb_name + '.' + args.image_set
    imdb = get_imdb(imdb_name)
    print 'Loaded dataset `{:s}` for training'.format(imdb.name)
    if args.k_fold: imdb.gen_k_fold(args.k_fold)
    # enhance roidb to contain flipped examples
    if cfg.TRAIN.USE_FLIPPED:
        print 'Appending horizontally-flipped training examples...'
        imdb.append_flipped_images()
        print 'done'
    #train folds
    if args.k_fold:
        print 'Training with {:d} folds'.format(args.k_fold)
        for k in range(args.k_fold):
            trainer = SVMTrainer(net, imdb,svm_cfg,k)
            trainer.train()
            trainer.save_sigmoids('{}/sigmoid_fold_{}.pkl'.format(out_dir,k))
            filename = '{}/svm_fold_{:d}.pkl'.format(out_dir,k)
            save_svm_coeffs(net,filename)
            print 'Wrote svm model to: {:s}'.format(filename)
    #train on full training set
    else:
        trainer = SVMTrainer(net, imdb,svm_cfg)
        trainer.train()
        trainer.save_sigmoids('{}/sigmoid.pkl'.format(out_dir))
        filename = '{}/svm.pkl'.format(out_dir)
        save_svm_coeffs(net,filename)
        print 'Wrote svm model to: {:s}'.format(filename)
