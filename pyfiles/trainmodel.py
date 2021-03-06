import xgboost as xgb
import itertools
import os
import csv
import sys
import Bio
import pandas as pd
from pandas import DataFrame
from Bio import Seq, SeqIO
import numpy as np
from tensorflow import keras
from keras.layers import Dense, Dropout
from keras.models import Sequential
from keras_tuner import RandomSearch, Hyperband
import keras_tuner as kt
import tensorflow as tf
from collections import Counter
from sklearn.model_selection import StratifiedKFold
from sklearn.preprocessing import LabelEncoder
from sklearn.feature_selection import SelectKBest, f_classif
from sklearn.metrics import precision_recall_fscore_support
from numpy import save, savetxt, load, loadtxt

def model_eval(predict, label):
    #from acheron
    accuracy = np.sum([predict[i]==label[i] for i in range(len(predict))])/len(predict)

    return accuracy

def load_models(modelnums):
    models = []
    features = []
    labels = []
    for num in modelnums:
        bst = xgb.Booster()
        file = str(num) + '.model'
        filen = str(num) + '.npy'
        bst.load_model(file)
        temp = np.load(filen, allow_pickle=True)
        models.append(bst)
        features.append(temp.item().get('features'))
        labels.append(temp.item().get('labels'))

    return models, features, labels

def load_data(dataloc):
    datapth = dataloc + '/processed_data/features.npy'
    labelpth = dataloc + '/processed_data/clean.csv'
    data = np.load(datapth, allow_pickle=True)
    colnames = ['id', 'assembly', 'genus', 'species', 'seqfile', 'cntfile']
    labels = pd.read_csv(labelpth, names=colnames)
    labels = labels.species.tolist()
    labels = np.asarray(labels)
    label_encoder = LabelEncoder()
    label_encoder = label_encoder.fit(labels)
    labels_encoded = label_encoder.transform(labels)

    return data, labels_encoded, label_encoder.classes_


def train_model(k, features, labels, unencoded_labels, save, datadir):
    """
    k - amount of folds if doing cross fold validation (1 if not)
    features - x_train
    labels = y_train
    params - model parameters
    save - true to save models, false if not saving, also saves test data fold for accompanying model
    """
    params = {'objective':'multi:softmax', 'num_class': '14', 'max_depth': '6', 'tree_method': 'hist'}
    count = 0
    num_feats = 1000
    kf = StratifiedKFold(n_splits=5, shuffle=True)
    final_models = []
    final_features = []
    final_labels = []
    for train_index, test_index in kf.split(features, labels):
        count+=1
        sk_obj = SelectKBest(f_classif, k=num_feats)
        Xtrain = features[train_index]
        Xtest = features[test_index]
        Ytrain = labels[train_index]
        Ytest = labels[test_index]
        Xtrain = sk_obj.fit_transform(Xtrain, Ytrain)
        Xtest = sk_obj.transform(Xtest)
        xgb_matrix = xgb.DMatrix(Xtrain, label=Ytrain)
        booster = xgb.train(params, xgb_matrix)
        final_models.append(booster)
        if save == True:
            modelsave = datadir + '/processed_data/' + str(count) + '.model'
            datasave = datadir + '/processed_data/' + str(count) + '.npy'
            booster.save_model(modelsave)
            saved_data = {'features':Xtest, 'labels':Ytest}
            np.save(datasave, saved_data)
        final_features.append(Xtest)
        final_labels.append(Ytest)
    return final_models, final_features, final_labels

def test_model(final_models, final_features, final_labels, labels_unencoded, datadir):
    count = 0
    for model, xtest, ytest in zip(final_models, final_features, final_labels):
        count+=1
        xgb_test_matrix = xgb.DMatrix(xtest)
        prediction = model.predict(xgb_test_matrix)
        score = model_eval(prediction, ytest)
        prec_recall = precision_recall_fscore_support(ytest, prediction, average=None)
        prec_recall = np.transpose(prec_recall)
        prec_recall = pd.DataFrame(data=prec_recall, index=labels_unencoded, columns=['Precision','Recall','F-Score','Supports'])
        model_report = datadir + '/processed_data/' + str(count) + 'summary.csv'
        prec_recall.to_csv(model_report)
    return

def build_model(hp):
    model = keras.Sequential()
    model.add(
        Dense(
             units=hp.Int("units", min_value=32, max_value=512, step=32),
            activation="relu", input_dim=1000
        )
    )
    model.add(Dropout(0.5))
    model.add(Dense(14, kernel_initializer='uniform', activation='softmax'))
    model.compile(
        optimizer=keras.optimizers.Adam(
            hp.Choice("learning_rate", values=[1e-2, 1e-3, 1e-4])
        ),
        loss="sparse_categorical_crossentropy",
        metrics=["accuracy"],
    )

    return model


def train_keras(k, data, label_encoded_y, labels_unencoded):
    num_feats = 1000
    kf = StratifiedKFold(n_splits=5, shuffle=True)
    final_models = []
    final_features = []
    final_labels = []
    final_hps = []

    stop_early = tf.keras.callbacks.EarlyStopping(monitor='val_loss', patience=6)
    fold = 0
    for train_index, test_index in kf.split(data, label_encoded_y):
        tuner = Hyperband(
        build_model,
        objective="val_accuracy",
        max_epochs=10,
        factor=3,
        overwrite=True,
        directory="/Users/koitere/hyp",
        project_name="BCP",
    )
        fold += 1
        Xtrain = data[train_index]
        Xtest = data[test_index]
        Ytrain = label_encoded_y[train_index]
        Ytest = label_encoded_y[test_index]
        sk_obj = SelectKBest(f_classif, k=num_feats)
        Xtrain = sk_obj.fit_transform(Xtrain, Ytrain)
        Xtest = sk_obj.transform(Xtest)
        tuner.search(Xtrain, Ytrain, epochs=20, validation_split=0.25,callbacks=[stop_early], verbose=0)
        best_hps = tuner.get_best_hyperparameters(num_trials=1)[0]
        model = tuner.hypermodel.build(best_hps)
        final_hps.append(best_hps)
        final_features.append(Xtest)
        final_labels.append(Ytest)

        history = model.fit(Xtrain, Ytrain, epochs=50, validation_split=0.25, verbose = 0)
        val_acc_per_epoch = history.history['val_accuracy']
        best_epoch = val_acc_per_epoch.index(max(val_acc_per_epoch)) + 1
        hypermodel = tuner.hypermodel.build(best_hps)
        hypermodel.fit(Xtrain, Ytrain, epochs=best_epoch, validation_split=0.25, verbose = 0)
        final_models.append(hypermodel)

    return final_hps, final_models, final_features, final_labels

def test_keras(final_models, final_features, final_labels):
    highest_acc = 0
    best_model = []
    for model, Xtest, Ytest in zip(final_models, final_features, final_labels):
        eval_result = model.evaluate(Xtest, Ytest, verbose = 0)
        print("[test loss, test accuracy]:", eval_result)
        if eval_result[1] > highest_acc:
            highest_acc = eval_result[1]
            best_model = model

    return best_model



#params = {'objective':'multi:softmax', 'num_class': '14', 'max_depth': '6', 'tree_method': 'hist'}

#models_loaded = True

#final_models = []
#final_features = []
#final_labels = []
#data, label_encoded_y, labels_unencoded = load_data(data_path,labels_path)
#inal_hps, final_models, final_features, final_labels = train_keras(5, data, label_encoded_y, labels_unencoded)
#test_keras(final_models, final_features, final_labels)

"""
if models_loaded == True:
    model_nums = [1,2,3,4,5]
    final_models, final_features, final_labels = load_models(model_nums)
    _,__,labels_unencoded = load_data(data_path,labels_path)
else:
    data,label_encoded_y, labels_unencoded = load_data(data_path, labels_path)
    final_models, final_features, final_labels = train_model(5, data, label_encoded_y, params, labels_unencoded, True)

test_model(final_models, final_features, final_labels, labels_unencoded)
"""
