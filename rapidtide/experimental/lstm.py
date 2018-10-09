#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Sat Jul 28 23:01:07 2018

@author: neuro
"""

import matplotlib.pyplot as plt
import dataload as dl
import numpy as np

from keras.models import Sequential
from keras.optimizers import RMSprop
from keras.layers import Bidirectional, Convolution1D, Dense, Activation, Dropout, BatchNormalization, LSTM, Flatten


def lstm(window_size=128,
        num_layers=3,
        num_units=1,
        dropout_rate=0.3,
        excludethresh=4.0,
        num_epochs=3,
        dofft=False,
        thesuffix='25.0Hz',
        modelname='model',
        thedatadir='/data1/frederic/test/output',
        readlim=None,
        countlim=None):

    folder = './batch/'
    print('lstm - loading data')
    if dofft:
        train_x, train_y, val_x, val_y, Ns, tclen, thebatchsize, dummy, dummy = dl.prep(window_size,
                                                                                      thesuffix=thesuffix,
                                                                                      thedatadir=thedatadir,
                                                                                      dofft=True,
                                                                                      readlim=readlim,
                                                                                      countlim=countlim)
    else:
        train_x, train_y, val_x, val_y, Ns, tclen, thebatchsize = dl.prep(window_size,
                                                                          thesuffix=thesuffix,
                                                                          thedatadir=thedatadir,
                                                                          dofft=False,
                                                                          readlim=readlim,
                                                                          countlim=countlim)

    print('dimension of input data', train_x.shape)
    print('dimension of output data', train_y.shape)
    model = Sequential()

    model.add(LSTM(num_units , activation='tanh', input_shape=(train_x.shape[1], train_x.shape[2],), recurrent_activation='hard_sigmoid'))
    #model.add(Dropout(rate=dropout_rate))
    #model.add(Flatten())
    #model.add(Dense(window_size, input_shape=(train_x.shape[1],), activation='linear'))
    model.compile (loss ="mean_squared_error" , optimizer="adam")  
    history = model.fit(train_x, train_y,
                        batch_size=train_x.shape[0],
                        epochs=num_epochs,
                        shuffle=False,
                        validation_data=(val_x, val_y))

    # save the model structure to a json file
    model_json = model.to_json()
    with open(modelname + ".json", "w") as json_file:
        json_file.write(model_json)

    # serialize weights to HDF5
    model.save_weights(modelname + '_weights.h5')

    YPred = model.predict(val_x)

    error = val_y - YPred
    sq_error = (np.mean(np.square(error)))

    error2 = val_x - val_y
    sq_error2 = (np.mean(np.square(error2)))
    description = ' '.join([
        'Num layers: ', str(num_layers),
        'Num filters: ', str(num_filters),
        'Dropout prob: ', str(dropout_rate),
        'Window size: ', str(window_size)
    ])
    print(description)
    print('Prediction Error: ', sq_error, 'Raw Error: ', sq_error2)

    f = open("loss.txt", "a")
    f.write(description + '\n')
    f.write('Prediction Error: ' + str(sq_error) + ' Raw Error: ' + str(sq_error2) + '\n')
    f.close()

    loss = history.history['loss']
    val_loss = history.history['val_loss']

    epochs = range(len(loss))

    plt.plot(epochs, loss, 'bo', label='Training loss')
    plt.plot(epochs, val_loss, 'b', label='Validation loss')
    plt.title('Training and validation loss')
    plt.legend()
    plt.savefig(
        folder + 'loss' + '_layer_' + str(num_layers) + '_filter_num_' + str(num_filters) + '_dropout_rate_' + str(
            dropout_rate) + '_window_size_' + str(window_size) + '.png')
    plt.close()

    # print('loss, val_loss', loss, val_loss)
    return loss, val_loss
