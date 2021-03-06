# coding: utf-8

# In[ ]:

import numpy as np
import chainer
from chainer import cuda, Function, gradient_check, report, training, utils, Variable
from chainer import datasets, iterators, optimizers, serializers
from chainer import Link, Chain, ChainList
import chainer.functions as F
import chainer.links as L
from chainer.training import extensions
from tqdm import tqdm
import sys
import os
from collections import Counter
import math
import pickle
import matplotlib.pyplot as plt
import seaborn as sns
import csv
import time
import matplotlib.gridspec as gridspec
import importlib
# %matplotlib inline


# ### Load configuration

# In[ ]:

from nmt_config import *
# reload(nmt_config)
# %load_ext autoreload
# %autoreload 2

# Special vocabulary symbols - we always put them at the start.
_PAD = b"_PAD"
_GO = b"_GO"
_EOS = b"_EOS"
_UNK = b"_UNK"
_START_VOCAB = [_PAD, _GO, _EOS, _UNK]

# ### Load Encoder Decoder implementation

# In[ ]:

from enc_dec_batch import *


# ### All experiments in this assignment can be trained on CPUs

# In[ ]:

# if >= 0, use GPU, if negative use CPU
xp = cuda.cupy if gpuid >= 0 else np


# ### Load integer id mappings

# In[ ]:
w2i = pickle.load(open(w2i_path, "rb"))
i2w = pickle.load(open(i2w_path, "rb"))
vocab = pickle.load(open(vocab_path, "rb"))
vocab_size_en = min(len(i2w["en"]), max_vocab_size["en"])
vocab_size_fr = min(len(i2w["fr"]), max_vocab_size["fr"])
print("vocab size, en={0:d}, fr={1:d}".format(vocab_size_en, vocab_size_fr))
# ### Setup Model

# In[ ]:

# Set up model
model = EncoderDecoder(vocab_size_fr, vocab_size_en,
                       num_layers_enc, num_layers_dec, num_layers_highway,
                       hidden_units, gpuid, segment_size, num_filters_conv, attn=use_attn, convolutional=CONVOLUTIONAL)
if gpuid >= 0:
    cuda.get_device(gpuid).use()
    model.to_gpu()

optimizer = optimizers.Adam()
optimizer.setup(model)
# gradient clipping
optimizer.add_hook(chainer.optimizer.GradientClipping(threshold=5))


# In[ ]:

print(log_train_fil_name)
print(model_fil)


# In[ ]:
def create_buckets():
    buck_width = BUCKET_WIDTH
    buckets = [[] for i in range(NUM_BUCKETS)]
    print("Splitting data into {0:d} buckets, each of width={1:d}".format(NUM_BUCKETS, buck_width))
    with open(text_fname["fr"], "rb") as fr_file, open(text_fname["en"], "rb") as en_file:
        for i, (line_fr, line_en) in enumerate(zip(fr_file, en_file), start=1):
            if i > NUM_TRAINING_SENTENCES:
                break
            if CONVOLUTIONAL:
                fr_sent = list(line_fr)
                en_sent = list(line_en)
            else:
                fr_sent = line_fr.strip().split()
                en_sent = line_en.strip().split()

            if len(fr_sent) > 0 and len(en_sent) > 0:
                max_len = min(max(len(fr_sent), len(en_sent)),
                              BUCKET_WIDTH * NUM_BUCKETS)
                buck_indx = ((max_len-1) // buck_width)

                fr_ids = [w2i["fr"].get(w, UNK_ID) for w in fr_sent[:max_len]]
                en_ids = [w2i["en"].get(w, UNK_ID) for w in en_sent[:max_len]]

                buckets[buck_indx].append((fr_ids, en_ids))

    # Saving bucket data
    print("Saving bucket data")
    for i, bucket in enumerate(buckets):
        print("Bucket {0:d}, # items={1:d}".format((i+1)*BUCKET_WIDTH, len(bucket)))
        pickle.dump(bucket, open(bucket_data_fname.format(i+1), "wb"))

    #return buckets


# In[ ]:
def compute_prec_recall():
    metrics = predict(s=NUM_TRAINING_SENTENCES,
                      num=NUM_DEV_SENTENCES, display=False, plot=False)

    prec = np.sum(metrics["cp"]) / np.sum(metrics["tp"])
    rec = np.sum(metrics["cp"]) / np.sum(metrics["t"])
    f_score = 2 * (prec * rec) / (prec + rec)

    print("{0:s}".format("-"*50))
    print("{0:s} | {1:0.4f}".format("precision", prec))
    print("{0:s} | {1:0.4f}".format("recall", rec))
    print("{0:s} | {1:0.4f}".format("f1", f_score))

# In[ ]:
def compute_dev_pplx():
    loss = 0
    num_words = 0
    with open(text_fname["fr"], "rb") as fr_file, open(text_fname["en"], "rb") as en_file:
        with tqdm(total=NUM_DEV_SENTENCES) as pbar:
            sys.stderr.flush()
            out_str = "loss={0:.6f}".format(0)
            pbar.set_description(out_str)
            for i, (line_fr, line_en) in enumerate(zip(fr_file, en_file), start=1):
                if i > NUM_TRAINING_SENTENCES and i <= (NUM_TRAINING_SENTENCES + NUM_DEV_SENTENCES):

                    if CONVOLUTIONAL:
                        fr_sent = list(line_fr)
                        en_sent = list(line_en)
                    else:
                        fr_sent = line_fr.strip().split()
                        en_sent = line_en.strip().split()


                    fr_ids = [w2i["fr"].get(w, UNK_ID) for w in fr_sent]
                    en_ids = [w2i["en"].get(w, UNK_ID) for w in en_sent]

                    # compute loss
                    curr_loss = float(model.encode_decode_train(fr_ids, en_ids, train=False).data)
                    loss += curr_loss
                    num_words += len(en_ids)

                    out_str = "loss={0:.6f}".format(curr_loss)
                    pbar.set_description(out_str)
                    pbar.update(1)

            # end of for
        # end of pbar
    # end of with open file
    loss_per_word = loss / num_words
    pplx = 2 ** loss_per_word
    random_pplx = vocab_size_en

    print("{0:s}".format("-"*50))
    print("{0:s} | {1:0.6f}".format("dev perplexity", pplx))
    print("{0:s} | {1:6d}".format("# words in dev", num_words))
    print("{0:s}".format("-"*50))

    return pplx


# ### Evaluation
#
# Bleu score

# In[ ]:

def bleu_stats(hypothesis, reference):
    yield len(hypothesis)
    yield len(reference)
    for n in range(1,5):
        s_ngrams = Counter([tuple(hypothesis[i:i+n]) for i in range(len(hypothesis)+1-n)])
        r_ngrams = Counter([tuple(reference[i:i+n]) for i in range(len(reference)+1-n)])
        yield max([sum((s_ngrams & r_ngrams).values()), 0])
        yield max([len(hypothesis)+1-n, 0])


# Compute BLEU from collected statistics obtained by call(s) to bleu_stats
def bleu(stats):
    if len(list(filter(lambda x: x==0, stats))) > 0:
        return 0
    (c, r) = stats[:2]
    log_bleu_prec = sum([math.log(float(x)/y) for x,y in zip(stats[2::2],stats[3::2])]) / 4.
    return math.exp(min([0, 1-float(r)/c]) + log_bleu_prec)

def compute_dev_bleu():
    list_of_references = []
    list_of_hypotheses = []
    with open(text_fname["fr"], "rb") as fr_file, open(text_fname["en"], "rb") as en_file:
        with tqdm(total=NUM_DEV_SENTENCES) as pbar:
            sys.stderr.flush()
            for i, (line_fr, line_en) in enumerate(zip(fr_file, en_file), start=1):
                if i > NUM_TRAINING_SENTENCES and i <= (NUM_TRAINING_SENTENCES + NUM_DEV_SENTENCES):

                    out_str = "predicting sentence={0:d}".format(i)
                    pbar.update(1)

                    if CONVOLUTIONAL:
                        fr_sent = list(line_fr)
                        en_sent = list(line_en)
                    else:
                        fr_sent = line_fr.strip().split()
                        en_sent = line_en.strip().split()

                    fr_ids = [w2i["fr"].get(w, UNK_ID) for w in fr_sent]
                    en_ids = [w2i["en"].get(w, UNK_ID) for w in en_sent]

                    # list_of_references.append(line_en.strip().split().decode())
                    reference_words = [w.decode() for w in line_en.strip().split()]
                    list_of_references.append(reference_words)
                    pred_sent, alpha_arr = model.encode_decode_predict(fr_ids)
                    pred_words = [i2w["en"][w].decode() for w in pred_sent if w != EOS_ID]
                    # pred_sent_line = " ".join(pred_words)
                    # list_of_hypotheses.append(pred_sent_line)
                    list_of_hypotheses.append(pred_words)
                if i > (NUM_TRAINING_SENTENCES + NUM_DEV_SENTENCES):
                    break

    stats = [0 for i in range(10)]
    for (r,h) in zip(list_of_references, list_of_hypotheses):
        stats = [sum(scores) for scores in zip(stats, bleu_stats(h,r))]
    print("BLEU: %0.2f" % (100 * bleu(stats)))

    return (100 * bleu(stats))



# ### Training loop

# In[ ]:

def train_loop(text_fname, num_training, num_epochs, log_mode="a"):
    # Set up log file for loss
    log_train_fil = open(log_train_fil_name, mode=log_mode)
    log_train_csv = csv.writer(log_train_fil, lineterminator="\n")

    log_dev_fil = open(log_dev_fil_name, mode=log_mode)
    log_dev_csv = csv.writer(log_dev_fil, lineterminator="\n")

    # initialize perplexity on dev set
    # save model when new epoch value is lower than previous
    pplx = float("inf")

    sys.stderr.flush()

    for epoch in range(num_epochs):
        with open(text_fname["fr"], "rb") as fr_file, open(text_fname["en"], "rb") as en_file:
            with tqdm(total=num_training) as pbar:
                sys.stderr.flush()
                loss_per_epoch = 0
                out_str = "epoch={0:d}, iter={1:d}, loss={2:.6f}, mean loss={3:.6f}".format(
                                epoch+1, 0, 0, 0)
                pbar.set_description(out_str)

                for i, (line_fr, line_en) in enumerate(zip(fr_file, en_file), start=1):

                    if CONVOLUTIONAL:
                        fr_sent = list(line_fr)
                        en_sent = list(line_en)
                    else:
                        fr_sent = line_fr.strip().split()
                        en_sent = line_en.strip().split()

                    fr_ids = [w2i["fr"].get(w, UNK_ID) for w in fr_sent]
                    en_ids = [w2i["en"].get(w, UNK_ID) for w in en_sent]

                    it = (epoch * NUM_TRAINING_SENTENCES) + i

                    if i > num_training:
                        break

                    # compute loss
                    loss = model.encode_decode_train(fr_ids, en_ids)

                    # set up for backprop
                    model.cleargrads()
                    loss.backward()
                    # update parameters
                    optimizer.update()
                    # store loss value for display
                    loss_val = float(loss.data)
                    loss_per_epoch += loss_val

                    out_str = "epoch={0:d}, iter={1:d}, loss={2:.6f}, mean loss={3:.6f}".format(
                               epoch+1, it, loss_val, (loss_per_epoch / i))
                    pbar.set_description(out_str)
                    pbar.update(1)


                    # log every 100 sentences
                    if i % 100 == 0:
                        log_train_csv.writerow([it, loss_val])

        print("finished training on {0:d} sentences".format(num_training))
        metrics = predict(s=NUM_TRAINING_SENTENCES,
                          num=NUM_DEV_SENTENCES, display=False, plot=False)

        prec = np.sum(metrics["cp"]) / np.sum(metrics["tp"])
        rec = np.sum(metrics["cp"]) / np.sum(metrics["t"])
        f_score = 2 * (prec * rec) / (prec + rec)

        print("{0:s}".format("-"*50))
        print("{0:s} | {1:0.4f}".format("precision", prec))
        print("{0:s} | {1:0.4f}".format("recall", rec))
        print("{0:s} | {1:0.4f}".format("f1", f_score))
        print("{0:s}".format("-"*50))
        print("computing perplexity")
        pplx_new = compute_dev_pplx()
        print("Saving model")
        serializers.save_npz(model_fil.replace(".model", "_{0:d}.model".format(epoch+1)), model)
        print("Finished saving model")
        pplx = pplx_new
        print("wooohooo!")
        print(log_train_fil_name)
        print(log_dev_fil_name)
        print(model_fil.replace(".model", "_{0:d}.model".format(epoch+1)))

        if epoch % 2 == 0:
            # print("Simple predictions (╯°□°）╯︵ ┻━┻")
            # print("training set predictions")
            # _ = predict(s=0, num=5, plot=False)
            # print("Simple predictions (╯°□°）╯︵ ┻━┻")
            # print("dev set predictions")
            # _ = predict(s=NUM_TRAINING_SENTENCES, num=5, plot=False)
            _ = compute_dev_bleu()
        # log pplx and bleu score
        log_dev_csv.writerow([(epoch+1), pplx_new, bleu_score])

    print("Final saving model")
    serializers.save_npz(model_fil, model)
    print("Finished saving model")

    print("Simple predictions")
    print("training set predictions")
    _ = predict(s=0, num=2, plot=False)
    print("Simple predictions")
    print("dev set predictions")
    _ = predict(s=NUM_TRAINING_SENTENCES, num=3, plot=False)
    print("{0:s}".format("-"*50))
    _ = compute_dev_bleu()
    print("{0:s}".format("-"*50))



    # close log file
    log_train_fil.close()
    log_dev_fil.close()
    print(log_train_fil_name)
    print(log_dev_fil_name)
    print(model_fil)



# In[ ]:
def batch_train_loop(bucket_fname, num_epochs,
                     batch_size=10, num_buckets=NUM_BUCKETS,
                     num_training=2,
                     bucket_width=BUCKET_WIDTH, log_mode="a", last_epoch_id=0):

    # Set up log file for loss
    log_train_fil = open(log_train_fil_name, mode=log_mode)
    log_train_csv = csv.writer(log_train_fil, lineterminator="\n")

    log_dev_fil = open(log_dev_fil_name, mode=log_mode)
    log_dev_csv = csv.writer(log_dev_fil, lineterminator="\n")

    # initialize perplexity on dev set
    # save model when new epoch value is lower than previous
    pplx = float("inf")
    bleu_score = 0

    sys.stderr.flush()

    for epoch in range(num_epochs):
        train_count = 0
        with tqdm(total=num_training) as pbar:
            sys.stderr.flush()
            loss_per_epoch = 0
            out_str = "epoch={0:d}, iter={1:d}, loss={2:.4f}, mean loss={3:.4f}, bucket={4:d}".format(
                            epoch+1, 0, 0, 0,0)
            pbar.set_description(out_str)

            for buck_indx in range(num_buckets):
                bucket_data = pickle.load(open(bucket_data_fname.format(buck_indx+1), "rb"))
                buck_pad_lim = (buck_indx+1) * bucket_width

                for i in range(0, len(bucket_data), batch_size):
                    if train_count >= num_training:
                        break
                    next_batch_end = min(batch_size, (num_training-train_count))
                    #print("current batch")
                    #print(bucket_data[i:i+next_batch_end])
                    #print("bucket limit", buck_pad_lim)
                    curr_len = len(bucket_data[i:i+next_batch_end])

                    loss = model.encode_decode_train_batch(bucket_data[i:i+next_batch_end],
                                                          buck_pad_lim, buck_pad_lim)
                    train_count += curr_len

                    # set up for backprop
                    model.cleargrads()
                    loss.backward()
                    # update parameters
                    optimizer.update()
                    # store loss value for display
                    loss_val = float(loss.data)
                    loss_per_epoch += loss_val

                    it = (epoch * NUM_TRAINING_SENTENCES) + curr_len

                    out_str = "epoch={0:d}, iter={1:d}, loss={2:.4f}, mean loss={3:.4f}, bucket={4:d}".format(
                               epoch+1, it, loss_val, (loss_per_epoch / (i+1)), (buck_indx+1))
                    pbar.set_description(out_str)
                    pbar.update(curr_len)

                    # log every 10 batches
                    if i % 10 == 0:
                        log_train_csv.writerow([it, loss_val])

                if train_count >= num_training:
                    break

        print("finished training on {0:d} sentences".format(num_training))
        print("{0:s}".format("-"*50))
        print("computing perplexity")
        pplx_new = compute_dev_pplx()
        print("Saving model")
        serializers.save_npz(model_fil.replace(".model", "_{0:d}.model".format(last_epoch_id+epoch+1)), model)
        print("Finished saving model")
        pplx = pplx_new
        print("wooohooo!")
        print(log_train_fil_name)
        print(log_dev_fil_name)
        print(model_fil.replace(".model", "_{0:d}.model".format(epoch+1)))

        if epoch % 2 == 0:
            bleu_score = compute_dev_bleu()

        # log pplx and bleu score
        log_dev_csv.writerow([(last_epoch_id+epoch+1), pplx_new, bleu_score])
        log_train_fil.flush()
        log_dev_fil.flush()
    print("Simple predictions")
    print("training set predictions")
    _ = predict(s=0, num=2, plot=False)
    print("Simple predictions")
    print("dev set predictions")
    _ = predict(s=NUM_TRAINING_SENTENCES, num=3, plot=False)
    print("{0:s}".format("-"*50))
    compute_dev_bleu()
    print("{0:s}".format("-"*50))

    print("Final saving model")
    serializers.save_npz(model_fil, model)
    print("Finished saving model")

    # close log file
    log_train_fil.close()
    log_dev_fil.close()
    print(log_train_fil_name)
    print(log_dev_fil_name)
    print(model_fil)

# ### Utilities

# In[ ]:

def load_model(model_fname, model):
    if os.path.exists(model_fname):
        print("Loading model file: {0:s}".format(model_fname))
        serializers.load_npz(model_fname, model)
    else:
        print("model file: {0:s} not found".format(model_fname))
    return model


# In[ ]:

from matplotlib.font_manager import FontProperties
'''
Japanese font needs to be downloaded.
Refer to http://stackoverflow.com/questions/23197124/display-non-ascii-japanese-characters-in-pandas-plot-legend
And download from:
http://ipafont.ipa.go.jp/old/ipafont/download.html#en
http://ipafont.ipa.go.jp/old/ipafont/IPAfont00303.php
'''
def plot_attention(alpha_arr, fr, en, plot_name=None):
    if gpuid >= 0:
        alpha_arr = cuda.to_cpu(alpha_arr).astype(np.float32)

    #alpha_arr /= np.max(np.abs(alpha_arr),axis=0)
    fig = plt.figure()
    fig.set_size_inches(8, 8)


    gs = gridspec.GridSpec(2, 2, width_ratios=[12,1],height_ratios=[12,1])

    ax = plt.subplot(gs[0])
    ax_c = plt.subplot(gs[1])

    cmap = sns.light_palette((200, 75, 60), input="husl", as_cmap=True)
    #prop = FontProperties(fname='fonts/IPAfont00303/ipam.ttf', size=12)
    ax = sns.heatmap(alpha_arr, xticklabels=fr, yticklabels=en, ax=ax, cmap=cmap, cbar_ax=ax_c)

    ax.xaxis.tick_top()
    ax.yaxis.tick_right()

    ax.set_xticklabels(en, minor=True, rotation=60, size=12)
    for label in ax.get_xticklabels(minor=False):
        label.set_fontsize(12)
        #label.set_font_properties(prop)

    for label in ax.get_yticklabels(minor=False):
        label.set_fontsize(12)
        label.set_rotation(-90)
        label.set_horizontalalignment('left')

    ax.set_xlabel("Source", size=20)
    ax.set_ylabel("Hypothesis", size=20)

    if plot_name:
        fig.savefig(plot_name, format="png")


#
# ### Predict
#
# ```
# Function to make predictions.
# s    : starting index of the line in the parallel data from which to make predictions
# num  : number of lines starting from "s" to make predictions for
# plot : plot attention if True
# ```

# In[ ]:

def predict_sentence(line_num, line_fr, line_en=None, display=True, plot_name=None, p_filt=0, r_filt=0):
    if CONVOLUTIONAL:
        fr_sent = list(line_fr)
    else:
        fr_sent = line_fr.strip().split()
    fr_ids = [w2i["fr"].get(w, UNK_ID) for w in fr_sent]
    # english reference is optional. If provided, compute precision/recall
    if line_en:
        if CONVOLUTIONAL:
            en_sent = list(line_en)
        else:
            en_sent = line_en.strip().split()

        en_ids = [w2i["en"].get(w, UNK_ID) for w in en_sent]

    pred_ids, alpha_arr = model.encode_decode_predict(fr_ids)
    pred_words = [i2w["en"][w].decode() for w in pred_ids]

    prec = 0
    rec = 0
    filter_match = False

    matches = count_match(en_ids, pred_ids)
    if EOS_ID in pred_ids:
        pred_len = len(pred_ids)-1
    else:
        pred_len = len(pred_ids)
    # subtract 1 from length for EOS id
    prec = (matches/pred_len) if pred_len > 0 else 0
    rec = matches/len(en_ids)

    if display and (prec >= p_filt and rec >= r_filt):
        filter_match = True
        # convert raw binary into string
        fr_words = [w.decode() for w in fr_sent]

        print("{0:s}".format("-"*50))
        print("sentence: {0:d}".format(line_num))
        print("{0:s} | {1:80s}".format("Src", line_fr.strip().decode()))
        print("{0:s} | {1:80s}".format("Ref", line_en.strip().decode()))
        print("{0:s} | {1:80s}".format("Hyp", " ".join(pred_words)))

        print("{0:s}".format("-"*50))

        print("{0:s} | {1:0.4f}".format("precision", prec))
        print("{0:s} | {1:0.4f}".format("recall", rec))

        if plot_name and use_attn:
            plot_attention(alpha_arr, fr_words, pred_words, plot_name)

    return matches, len(pred_ids), len(en_ids), filter_match

# In[ ]:


def predict(s=NUM_TRAINING_SENTENCES, num=NUM_DEV_SENTENCES, display=True, plot=False, p_filt=0, r_filt=0):
    print("English predictions, s={0:d}, num={1:d}:".format(s, num))

    metrics = {"cp":[], "tp":[], "t":[]}

    filter_count = 0

    with open(text_fname["fr"], "rb") as fr_file, open(text_fname["en"], "rb") as en_file:
        for i, (line_fr, line_en) in enumerate(zip(fr_file, en_file), start=0):
            if i >= s and i < (s+num):
                if plot:
                    plot_name = os.path.join(model_dir, "sample_{0:d}_plot.png".format(i+1))
                else:
                    plot_name=None

                # make prediction
                cp, tp, t, f = predict_sentence(i, line_fr,
                                             line_en,
                                             display=display,
                                             plot_name=plot_name,
                                             p_filt=p_filt, r_filt=r_filt)
                metrics["cp"].append(cp)
                metrics["tp"].append(tp)
                metrics["t"].append(t)
                filter_count += (1 if f else 0)

    print("sentences matching filter = {0:d}".format(filter_count))
    return metrics


# In[ ]:
def count_match(list1, list2):
    # each list can have repeated elements. The count should account for this.
    count1 = Counter(list1)
    count2 = Counter(list2)
    count2_keys = count2.keys()-set([UNK_ID, EOS_ID])
    common_w = set(count1.keys()) & set(count2_keys)
    #all_w = set(count1.keys()) + set(count2.keys())
    matches = sum([min(count1[w], count2[w]) for w in common_w])
    #matches = sum([max(0, count2[v]-count1[v]) for v in (count2-count1).values()])
    #matches = sum([max(0, count2[v]-count1[v]) for v in common_w])
    return matches
#     for w in all_w:
#         if w in common_w:
#     print(count1, count2)


# ### Check for existing model

# In[ ]:

# model, optimizer = setup_model()

def main():
    print("here", os.path.exists(model_fil))
    if create_buckets_flag:
        create_buckets()
    else:
        print("not creating buckets as requested. will crash if buckets not present")

    max_epoch_id = 0
    if os.path.exists(model_fil):
        # check last saved epoch model:
        for fname in [f for f in os.listdir(model_dir) if f.endswith("")]:
            if model_fil != os.path.join(model_dir, fname) and model_fil.replace(".model", "") in os.path.join(model_dir, fname):
                try:
                    epoch_id = int(fname.split("_")[-1].replace(".model", ""))
                    if epoch_id > max_epoch_id:
                        max_epoch_id = epoch_id
                except:
                    print("{0:s} not a valid model file".format(fname))
        print("last saved epoch model={0:d}".format(max_epoch_id))

        if load_existing_model:
            print("loading model ...")
            serializers.load_npz(model_fil, model)
            print("finished loading: {0:s}".format(model_fil))
        else:
            print("""model file already exists!!
                Delete before continuing, or enable load_existing flag""".format(model_fil))
            return
    if NUM_EPOCHS > 0:
        #train_loop(text_fname, NUM_TRAINING_SENTENCES, NUM_EPOCHS)
        batch_train_loop(bucket_data_fname,
                 num_epochs=NUM_EPOCHS,
                 batch_size=BATCH_SIZE,
                 num_buckets=NUM_BUCKETS,
                 num_training=NUM_TRAINING_SENTENCES,
                 bucket_width=BUCKET_WIDTH, last_epoch_id=max_epoch_id)
        compute_dev_bleu()


if __name__ == "__main__":
    main()
