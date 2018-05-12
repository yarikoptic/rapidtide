#!/usr/bin/env python
#
#   Copyright 2016 Blaise Frederick
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
#
# $Author: frederic $
# $Date: 2016/07/11 14:50:43 $
# $Id: rapidtide,v 1.161 2016/07/11 14:50:43 frederic Exp $
#
#
#

from __future__ import print_function, division

import time
import getopt
import platform
import bisect
import warnings
import os
import sys
import gc
import multiprocessing as mp
from functools import partial
import resource

import rapidtide.tide_funcs as tide

from sklearn.decomposition import FastICA, PCA
from pylab import figure, plot, show
import numpy as np
from statsmodels.tsa.stattools import pacf_yw
from scipy.stats.stats import pearsonr
from scipy.signal import welch
from scipy import ndimage

try:
    from memory_profiler import profile

    memprofilerexists = True
except ImportError:
    memprofilerexists = False


def conditionalprofile():
    def resdec(f):
        if memprofilerexists:
            return profile(f)
        return f

    return resdec


global rt_floatset, rt_floattype


@conditionalprofile()
def memcheckpoint(message):
    print(message)


def startendcheck(timepoints, startpoint, endpoint):
    if startpoint > timepoints - 1:
        print('startpoint is too large (maximum is ', timepoints - 1, ')')
        sys.exit()
    if startpoint < 0:
        realstart = 0
        print('startpoint set to minimum, (0)')
    else:
        realstart = startpoint
        print('startpoint set to ', startpoint)
    if endpoint > timepoints - 1:
        realend = timepoints - 1
        print('endppoint set to maximum, (', timepoints - 1, ')')
    else:
        realend = endpoint
        print('endpoint set to ', endpoint)
    if realstart >= realend:
        print('endpoint (', realend, ') must be greater than startpoint (', realstart, ')')
        sys.exit()
    return realstart, realend


def procOneNullCorrelation(iteration, indata, ncprefilter, oversampfreq, corrscale, corrorigin, lagmininpts,
                           lagmaxinpts, optiondict):
    # make a shuffled copy of the regressors
    shuffleddata = np.random.permutation(indata)

    # crosscorrelate with original
    thexcorr, dummy = onecorrelation(shuffleddata, oversampfreq, corrorigin, lagmininpts, lagmaxinpts, ncprefilter, indata,
                              optiondict)

    # fit the correlation
    maxindex, maxlag, maxval, maxsigma, maskval, failreason = \
        onecorrfit(thexcorr, corrscale[corrorigin - lagmininpts:corrorigin + lagmaxinpts],
                   optiondict)

    return maxval


def getNullDistributionData(indata, corrscale, ncprefilter, oversampfreq, corrorigin, lagmininpts, lagmaxinpts,
                            optiondict):
    if optiondict['multiproc']:
        # define the consumer function here so it inherits most of the arguments
        def nullCorrelation_consumer(inQ, outQ):
            while True:
                try:
                    # get a new message
                    val = inQ.get()

                    # this is the 'TERM' signal
                    if val is None:
                        break

                    # process and send the data
                    outQ.put(procOneNullCorrelation(val, indata, ncprefilter, oversampfreq, corrscale, corrorigin,
                                                    lagmininpts, lagmaxinpts, optiondict))

                except Exception as e:
                    print("error!", e)
                    break

        # initialize the workers and the queues
        n_workers = optiondict['nprocs']
        inQ = mp.Queue()
        outQ = mp.Queue()
        workers = [mp.Process(target=nullCorrelation_consumer, args=(inQ, outQ)) for i in range(n_workers)]
        for i, w in enumerate(workers):
            w.start()

        # pack the data and send to workers
        data_in = []
        for d in range(optiondict['numestreps']):
            data_in.append(d)
        print('processing', len(data_in), 'correlations with', n_workers, 'processes')
        data_out = process_data(data_in, inQ, outQ, showprogressbar=optiondict['showprogressbar'],
                                chunksize=optiondict['mp_chunksize'])

        # shut down workers
        for i in range(n_workers):
            inQ.put(None)
        for w in workers:
            w.terminate()
            w.join()

        # unpack the data
        volumetotal = 0
        corrlist = np.asarray(data_out, dtype=rt_floattype)
    else:
        corrlist = np.zeros((optiondict['numestreps']), dtype=rt_floattype)

        for i in range(0, optiondict['numestreps']):
            # make a shuffled copy of the regressors
            shuffleddata = np.random.permutation(indata)

            # crosscorrelate with original
            thexcorr, dummy = onecorrelation(shuffleddata, oversampfreq, corrorigin, lagmininpts, lagmaxinpts, ncprefilter,
                                      indata,
                                      optiondict)

            # fit the correlation
            maxindex, maxlag, maxval, maxsigma, maskval, failreason = \
                onecorrfit(thexcorr, corrscale[corrorigin - lagmininpts:corrorigin + lagmaxinpts],
                           optiondict)

            # find and tabulate correlation coefficient at optimal lag
            corrlist[i] = maxval

            # progress
            if optiondict['showprogressbar']:
                tide.progressbar(i + 1, optiondict['numestreps'], label='Percent complete')

        # jump to line after progress bar
        print()

    # return the distribution data
    return corrlist


def onecorrelation(thetc, oversampfreq, corrorigin, lagmininpts, lagmaxinpts, ncprefilter, referencetc, optiondict):
    thetc_classfilter = ncprefilter.apply(oversampfreq, thetc)
    thetc = thetc_classfilter

    # prepare timecourse by normalizing, detrending, and applying a window function
    preppedtc = tide.corrnormalize(thetc, optiondict['usewindowfunc'], optiondict['dodetrend'], windowfunc=optiondict['windowfunc'])

    # now actually do the correlation
    thexcorr = tide.fastcorrelate(preppedtc, referencetc, usefft=True, weighting=optiondict['corrweighting'])

    # find the global maximum value
    theglobalmax = np.argmax(thexcorr)

    return thexcorr[corrorigin - lagmininpts:corrorigin + lagmaxinpts], theglobalmax


def procOneVoxelCorrelation(vox, thetc, optiondict, fmri_x, fmritc, os_fmri_x, oversampfreq,
                            corrorigin, lagmininpts, lagmaxinpts, ncprefilter, referencetc):
    global rt_floattype
    if optiondict['oversampfactor'] >= 1:
        thetc[:] = tide.doresample(fmri_x, fmritc, os_fmri_x, method=optiondict['interptype'])
    else:
        thetc[:] = fmritc
    thexcorr, theglobalmax = onecorrelation(thetc, oversampfreq, corrorigin, lagmininpts, lagmaxinpts, ncprefilter,
                                               referencetc, optiondict)
    return vox, np.mean(thetc), thexcorr, theglobalmax


def process_data(data_in, inQ, outQ, showprogressbar=True, reportstep=1000, chunksize=10000):
    # send pos/data to workers
    data_out = []
    totalnum = len(data_in)
    numchunks = int(totalnum // chunksize)
    remainder = totalnum - numchunks * chunksize
    if showprogressbar:
        tide.progressbar(0, totalnum, label="Percent complete")

    # process all of the complete chunks
    for thechunk in range(numchunks):
        # queue the chunk
        for i, dat in enumerate(data_in[thechunk * chunksize:(thechunk + 1) * chunksize]):
            inQ.put(dat)
        offset = thechunk * chunksize

        # retrieve the chunk
        numreturned = 0
        while True:
            ret = outQ.get()
            if ret is not None:
                data_out.append(ret)
            numreturned += 1
            if (((numreturned + offset + 1) % reportstep) == 0) and showprogressbar:
                tide.progressbar(numreturned + offset + 1, totalnum, label="Percent complete")
            if numreturned > chunksize - 1:
                break

    # queue the remainder
    for i, dat in enumerate(data_in[numchunks * chunksize:numchunks * chunksize + remainder]):
        inQ.put(dat)
    numreturned = 0
    offset = numchunks * chunksize

    # retrieve the remainder
    while True:
        ret = outQ.get()
        if ret is not None:
            data_out.append(ret)
        numreturned += 1
        if (((numreturned + offset + 1) % reportstep) == 0) and showprogressbar:
            tide.progressbar(numreturned + offset + 1, totalnum, label="Percent complete")
        if numreturned > remainder - 1:
            break
    if showprogressbar:
        tide.progressbar(totalnum, totalnum, label="Percent complete")
    print()

    return data_out


def correlationpass(fmridata, fmrifftdata, referencetc,
                    fmri_x, os_fmri_x,
                    tr,
                    corrorigin, lagmininpts, lagmaxinpts,
                    corrmask, corrout, meanval,
                    ncprefilter,
                    optiondict):
    oversampfreq = optiondict['oversampfactor'] / tr
    inputshape = np.shape(fmridata)
    volumetotal = 0
    reportstep = 1000
    thetc = np.zeros(np.shape(os_fmri_x), dtype=rt_floattype)
    theglobalmaxlist = []
    if optiondict['multiproc']:
        # define the consumer function here so it inherits most of the arguments
        def correlation_consumer(inQ, outQ):
            while True:
                try:
                    # get a new message
                    val = inQ.get()

                    # this is the 'TERM' signal
                    if val is None:
                        break

                    # process and send the data
                    outQ.put(procOneVoxelCorrelation(val, thetc, optiondict, fmri_x, fmridata[val, :], os_fmri_x,
                                                     oversampfreq,
                                                     corrorigin, lagmininpts, lagmaxinpts, ncprefilter, referencetc))

                except Exception as e:
                    print("error!", e)
                    break

        # initialize the workers and the queues
        n_workers = optiondict['nprocs']
        inQ = mp.Queue()
        outQ = mp.Queue()
        workers = [mp.Process(target=correlation_consumer, args=(inQ, outQ)) for i in range(n_workers)]
        for i, w in enumerate(workers):
            w.start()

        # pack the data and send to workers
        data_in = []
        for d in range(inputshape[0]):
            data_in.append(d)
        print('processing', len(data_in), 'voxels with', n_workers, 'processes')
        data_out = process_data(data_in, inQ, outQ,  showprogressbar=optiondict['showprogressbar'],
                                chunksize=optiondict['mp_chunksize'])

        # shut down workers
        for i in range(n_workers):
            inQ.put(None)
        for w in workers:
            w.terminate()
            w.join()

        # unpack the data
        volumetotal = 0
        for voxel in data_out:
            # corrmask[voxel[0]] = 1
            meanval[voxel[0]] = voxel[1]
            corrout[voxel[0], :] = voxel[2]
            theglobalmaxlist.append(voxel[3] + 0)
            volumetotal += 1
        data_out = []
    else:
        for vox in range(0, inputshape[0]):
            if (vox % reportstep == 0 or vox == inputshape[0] - 1) and optiondict['showprogressbar']:
                tide.progressbar(vox + 1, inputshape[0], label='Percent complete')
            dummy, meanval[vox], corrout[vox, :], theglobalmax = procOneVoxelCorrelation(vox, thetc, optiondict, fmri_x,
                                                                           fmridata[vox, :], os_fmri_x, oversampfreq,
                                                                           corrorigin, lagmininpts, lagmaxinpts,
                                                                           ncprefilter, referencetc)
            theglobalmaxlist.append(theglobalmax + 0)
            volumetotal += 1
    print('\nCorrelation performed on ' + str(volumetotal) + ' voxels')

    # garbage collect
    collected = gc.collect()
    print("Garbage collector: collected %d objects." % collected)

    return volumetotal, theglobalmaxlist


def onecorrfit(thetc, corrscale, optiondict, displayplots=False, initiallag=None):
    if initiallag is not None:
        maxguess = initiallag
        useguess = True
        widthlimit = optiondict['despeckle_thresh']
    else:
        maxguess = 0.0
        useguess = False
        widthlimit = optiondict['widthlimit']

    if optiondict['bipolar']:
        if max(thetc) < -1.0 * min(thetc):
            flipfac = rt_floatset(-1.0)
        else:
            flipfac = rt_floatset(1.0)
    else:
        flipfac = rt_floatset(1.0)
    if not optiondict['fixdelay']:
        if optiondict['findmaxtype'] == 'gauss':
            maxindex, maxlag, maxval, maxsigma, maskval, failreason, peakstart, peakend = tide.findmaxlag_gauss(
                corrscale,
                flipfac * thetc,
                optiondict['lagmin'], optiondict['lagmax'], widthlimit,
                edgebufferfrac=optiondict['edgebufferfrac'],
                threshval=optiondict['lthreshval'],
                uthreshval=optiondict['uthreshval'],
                debug=optiondict['debug'],
                refine=optiondict['gaussrefine'],
                maxguess=maxguess,
                useguess=useguess,
                fastgauss=optiondict['fastgauss'],
                enforcethresh=optiondict['enforcethresh'],
                zerooutbadfit=optiondict['zerooutbadfit'],
                lagmod=optiondict['lagmod'],
                displayplots=displayplots)
        else:
            maxindex, maxlag, maxval, maxsigma, maskval, failreason, peakstart, peakend = tide.findmaxlag_quad(
                corrscale,
                flipfac * thetc,
                optiondict['lagmin'], optiondict['lagmax'], widthlimit,
                edgebufferfrac=optiondict['edgebufferfrac'],
                threshval=optiondict['lthreshval'],
                uthreshval=optiondict['uthreshval'],
                debug=optiondict['debug'],
                refine=optiondict['gaussrefine'],
                maxguess=maxguess,
                useguess=useguess,
                fastgauss=optiondict['fastgauss'],
                enforcethresh=optiondict['enforcethresh'],
                zerooutbadfit=optiondict['zerooutbadfit'],
                lagmod=optiondict['lagmod'],
                displayplots=displayplots)
        maxval *= flipfac
    else:
        # do something different
        failreason = np.int16(0)
        maxlag = rt_floatset(optiondict['fixeddelayvalue'])
        maxindex = np.int16(bisect.bisect_left(corrscale, optiondict['fixeddelayvalue']))
        maxval = rt_floatset(flipfac * thetc[maxindex])
        maxsigma = rt_floatset(1.0)
        maskval = np.uint16(1)

    return maxindex, maxlag, maxval, maxsigma, maskval, failreason


def procOneVoxelFitcorr(vox, corrtc, corrscale, genlagtc, initial_fmri_x, optiondict, displayplots, initiallag=None):
    if optiondict['slicetimes'] is not None:
        sliceoffsettime = optiondict['slicetimes'][vox % slicesize]
    else:
        sliceoffsettime = 0.0
    maxindex, maxlag, maxval, maxsigma, maskval, failreason = onecorrfit(corrtc, corrscale,
                                                                         optiondict, displayplots=displayplots,
                                                                         initiallag=initiallag)

    if maxval > 0.3:
        displayplots = False

    # question - should maxlag be added or subtracted?  As of 10/18, it is subtracted
    #  potential answer - tried adding, results are terrible.
    thelagtc = rt_floatset(genlagtc.yfromx(initial_fmri_x - maxlag))

    # now tuck everything away in the appropriate output array
    volumetotalinc = 0
    if (maskval == 0) and optiondict['zerooutbadfit']:
        thetime = rt_floatset(0.0)
        thestrength = rt_floatset(0.0)
        thesigma = rt_floatset(0.0)
        thegaussout = 0.0 * corrtc
        theR2 = rt_floatset(0.0)
    else:
        volumetotalinc = 1
        thetime = rt_floatset(np.fmod(maxlag, optiondict['lagmod']))
        thestrength = rt_floatset(maxval)
        thesigma = rt_floatset(maxsigma)
        if (not optiondict['fixdelay']) and (maxsigma != 0.0):
            thegaussout = rt_floatset(tide.gauss_eval(corrscale, [maxval, maxlag, maxsigma]))
        else:
            thegaussout = rt_floatset(0.0)
        theR2 = rt_floatset(thestrength * thestrength)

    return vox, volumetotalinc, thelagtc, thetime, thestrength, thesigma, thegaussout, theR2, maskval, failreason


def fitcorr(genlagtc, initial_fmri_x, lagtc, slicesize,
            corrscale, lagmask, lagtimes, lagstrengths, lagsigma, corrout, meanval, gaussout,
            R2, optiondict, initiallags=None):
    displayplots = False
    inputshape = np.shape(corrout)
    volumetotal, ampfails, lagfails, widthfails, edgefails, fitfails = 0, 0, 0, 0, 0, 0
    reportstep = 1000
    zerolagtc = rt_floatset(genlagtc.yfromx(initial_fmri_x))
    sliceoffsettime = 0.0

    if optiondict['multiproc']:
        # define the consumer function here so it inherits most of the arguments
        def fitcorr_consumer(inQ, outQ):
            while True:
                try:
                    # get a new message
                    val = inQ.get()

                    # this is the 'TERM' signal
                    if val is None:
                        break

                    # process and send the data
                    if initiallags is None:
                        outQ.put(procOneVoxelFitcorr(val, corrout[val, :], corrscale, genlagtc, initial_fmri_x, optiondict,
                                                 displayplots))
                    else:
                        outQ.put(procOneVoxelFitcorr(val, corrout[val, :], corrscale, genlagtc, initial_fmri_x, optiondict,
                                                 displayplots, initiallag=initiallags[val]))

                except Exception as e:
                    print("error!", e)
                    break

        # initialize the workers and the queues
        n_workers = optiondict['nprocs']
        inQ = mp.Queue()
        outQ = mp.Queue()
        workers = [mp.Process(target=fitcorr_consumer, args=(inQ, outQ)) for i in range(n_workers)]
        for i, w in enumerate(workers):
            w.start()

        # pack the data and send to workers
        data_in = []
        for d in range(inputshape[0]):
            if initiallags is None:
                data_in.append(d)
            else:
                if initiallags[d] > -1000000.0:
                    data_in.append(d)
        print('processing', len(data_in), 'voxels with', n_workers, 'processes')
        data_out = process_data(data_in, inQ, outQ,  showprogressbar=optiondict['showprogressbar'],
                                chunksize=optiondict['mp_chunksize'])

        # shut down workers
        for i in range(n_workers):
            inQ.put(None)
        for w in workers:
            w.terminate()
            w.join()

        # unpack the data
        volumetotal = 0
        for voxel in data_out:
            volumetotal += voxel[1]
            lagtc[voxel[0], :] = voxel[2]
            lagtimes[voxel[0]] = voxel[3]
            lagstrengths[voxel[0]] = voxel[4]
            lagsigma[voxel[0]] = voxel[5]
            gaussout[voxel[0], :] = voxel[6]
            R2[voxel[0]] = voxel[7]
            lagmask[voxel[0]] = voxel[8]
        data_out = []
    else:
        for vox in range(0, inputshape[0]):
            if (vox % reportstep == 0 or vox == inputshape[0] - 1) and optiondict['showprogressbar']:
                tide.progressbar(vox + 1, inputshape[0], label='Percent complete')
            if initiallags is None:
                dummy, volumetotalinc, lagtc[vox, :], lagtimes[vox], lagstrengths[vox], lagsigma[vox], gaussout[vox, :], R2[
                    vox], lagmask[vox], failreason = \
                    procOneVoxelFitcorr(vox, corrout[vox, :], corrscale, genlagtc, initial_fmri_x, optiondict, displayplots)
                volumetotal += volumetotalinc
            else:
                if initiallags[vox] != 0.0:
                    dummy, volumetotalinc, lagtc[vox, :], lagtimes[vox], lagstrengths[vox], lagsigma[vox], gaussout[vox, :], R2[
                        vox], lagmask[vox], failreason = \
                        procOneVoxelFitcorr(vox, corrout[vox, :], corrscale, genlagtc, initial_fmri_x, optiondict, displayplots, initiallags[vox])
                    volumetotal += volumetotalinc
    print('\nCorrelation fitted in ' + str(volumetotal) + ' voxels')
    print('\tampfails=', ampfails, ' lagfails=', lagfails, ' widthfail=', widthfails, ' edgefail=', edgefails,
          ' fitfail=', fitfails)

    # garbage collect
    collected = gc.collect()
    print("Garbage collector: collected %d objects." % collected)

    return volumetotal


def procOneVoxelTimeShift(vox, fmritc, optiondict, lagstrength, R2val, lagtime, padtrs, fmritr, theprefilter):
    if optiondict['refineprenorm'] == 'mean':
        thedivisor = np.mean(fmritc)
    elif optiondict['refineprenorm'] == 'var':
        thedivisor = np.var(fmritc)
    elif optiondict['refineprenorm'] == 'std':
        thedivisor = np.std(fmritc)
    elif optiondict['refineprenorm'] == 'invlag':
        if lagtime < optiondict['lagmaxthresh']:
            thedivisor = optiondict['lagmaxthresh'] - lagtime
        else:
            thedivisor = 0.0
    else:
        thedivisor = 1.0
    if thedivisor != 0.0:
        normfac = 1.0 / thedivisor
    else:
        normfac = 0.0

    if optiondict['refineweighting'] == 'R':
        thisweight = lagstrength
    elif optiondict['refineweighting'] == 'R2':
        thisweight = R2val
    else:
        thisweight = 1.0
    if optiondict['dodetrend']:
        normtc = tide.detrend(fmritc * normfac * thisweight, demean=True)
    else:
        normtc = fmritc * normfac * thisweight
    shifttr = -(-optiondict['offsettime'] + lagtime) / fmritr  # lagtime is in seconds
    [shiftedtc, weights, paddedshiftedtc, paddedweights] = tide.timeshift(normtc, shifttr, padtrs)
    if optiondict['filterbeforePCA']:
        outtc = theprefilter.apply(optiondict['fmrifreq'], shiftedtc)
        outweights = theprefilter.apply(optiondict['fmrifreq'], weights)
    else:
        outtc = 1.0 * shiftedtc
        outweights = 1.0 * weights
    if optiondict['psdfilter']:
        freqs, psd = welch(tide.corrnormalize(shiftedtc, True, True), fmritr, scaling='spectrum', window='hamming',
                           return_onesided=False, nperseg=len(shiftedtc))
        return vox, outtc, outweights, np.sqrt(psd)
    else:
        return vox, outtc, outweights, None


def refineregressor(reference, fmridata, fmritr, shiftedtcs, weights, passnum, lagstrengths, lagtimes,
                    lagsigma, R2,
                    theprefilter, optiondict, padtrs=60, includemask=None, excludemask=None):
    #print('entering refineregressor with padtrs=', padtrs)
    inputshape = np.shape(fmridata)
    ampmask = np.where(lagstrengths >= optiondict['ampthresh'], np.int16(1), np.int16(0))
    if optiondict['lagmaskside'] == 'upper':
        delaymask = \
            np.where(lagtimes > optiondict['lagminthresh'], np.int16(1), np.int16(0)) * \
            np.where(lagtimes < optiondict['lagmaxthresh'], np.int16(1), np.int16(0))
    elif optiondict['lagmaskside'] == 'lower':
        delaymask = \
            np.where(lagtimes < -optiondict['lagminthresh'], np.int16(1), np.int16(0)) * \
            np.where(lagtimes > -optiondict['lagmaxthresh'], np.int16(1), np.int16(0))
    else:
        abslag = abs(lagtimes)
        delaymask = \
            np.where(abslag > optiondict['lagminthresh'], np.int16(1), np.int16(0)) * \
            np.where(abslag < optiondict['lagmaxthresh'], np.int16(1), np.int16(0))
    sigmamask = np.where(lagsigma < optiondict['sigmathresh'], np.int16(1), np.int16(0))
    locationmask = 0 * ampmask + 1
    if includemask is not None:
        locationmask = locationmask * includemask
    if excludemask is not None:
        locationmask = locationmask * excludemask
    print('location mask created')

    # first generate the refine mask
    locationfails = np.sum(1 - locationmask)
    ampfails = np.sum(1 - ampmask)
    lagfails = np.sum(1 - delaymask)
    sigmafails = np.sum(1 - sigmamask)
    maskarray = locationmask * ampmask * delaymask * sigmamask
    volumetotal = np.sum(maskarray)
    reportstep = 1000

    # timeshift the valid voxels
    if optiondict['multiproc']:
        # define the consumer function here so it inherits most of the arguments
        def timeshift_consumer(inQ, outQ):
            while True:
                try:
                    # get a new message
                    val = inQ.get()

                    # this is the 'TERM' signal
                    if val is None:
                        break

                    # process and send the data
                    outQ.put(procOneVoxelTimeShift(val, fmridata[val, :], optiondict,
                                                   lagstrengths[val], R2[val], lagtimes[val], padtrs, fmritr,
                                                   theprefilter))

                except Exception as e:
                    print("error!", e)
                    break

        # initialize the workers and the queues
        n_workers = optiondict['nprocs']
        inQ = mp.Queue()
        outQ = mp.Queue()
        workers = [mp.Process(target=timeshift_consumer, args=(inQ, outQ)) for i in range(n_workers)]
        for i, w in enumerate(workers):
            w.start()

        # pack the data and send to workers
        data_in = []
        for d in range(inputshape[0]):
            if (maskarray[d] > 0) or optiondict['shiftall']:
                data_in.append(d)
        print('processing', len(data_in), 'voxels with', n_workers, 'processes')
        data_out = process_data(data_in, inQ, outQ,  showprogressbar=optiondict['showprogressbar'],
                                chunksize=optiondict['mp_chunksize'])

        # shut down workers
        for i in range(n_workers):
            inQ.put(None)
        for w in workers:
            w.terminate()
            w.join()

        # unpack the data
        psdlist = []
        for voxel in data_out:
            shiftedtcs[voxel[0], :] = voxel[1]
            weights[voxel[0], :] = voxel[2]
            if optiondict['psdfilter']:
                psdlist.append(voxel[3])
        data_out = []
    else:
        psdlist = []
        for vox in range(0, inputshape[0]):
            if (vox % reportstep == 0 or vox == inputshape[0] - 1) and optiondict['showprogressbar']:
                tide.progressbar(vox + 1, inputshape[0], label='Percent complete (timeshifting)')
            if (maskarray[vox] > 0) or optiondict['shiftall']:
                retvals = procOneVoxelTimeShift(vox, fmridata[vox, :], optiondict, lagstrengths[vox], R2[vox],
                                                lagtimes[vox], padtrs, fmritr, theprefilter)
                shiftedtcs[retvals[0], :] = retvals[1]
                weights[retvals[0], :] = retvals[2]
                if optiondict['psdfilter']:
                    psdlist.append(retvals[3])
        print()

    if optiondict['psdfilter']:
        print(len(psdlist))
        print(psdlist[0])
        print(np.shape(np.asarray(psdlist, dtype=rt_floattype)))
        averagepsd = np.mean(np.asarray(psdlist, dtype=rt_floattype), axis=0)
        stdpsd = np.std(np.asarray(psdlist, dtype=rt_floattype), axis=0)
        snr = np.nan_to_num(averagepsd / stdpsd)
        # fig = figure()
        # ax = fig.add_subplot(111)
        # ax.set_title('Average and stedev of PSD')
        # plot(averagepsd)
        # plot(stdpsd)
        # show()
        # fig = figure()
        # ax = fig.add_subplot(111)
        # ax.set_title('SNR')
        # plot(snr)
        # show()

    # now generate the refined timecourse(s)
    validlist = np.where(maskarray > 0)[0]
    refinevoxels = shiftedtcs[validlist]
    refineweights = weights[validlist]
    weightsum = np.sum(refineweights, axis=0) / volumetotal
    averagedata = np.sum(refinevoxels, axis=0) / volumetotal
    if optiondict['shiftall']:
        invalidlist = np.where((1 - ampmask) > 0)[0]
        discardvoxels = shiftedtcs[invalidlist]
        discardweights = weights[invalidlist]
        discardweightsum = np.sum(discardweights, axis=0) / volumetotal
        averagediscard = np.sum(discardvoxels, axis=0) / volumetotal
    if optiondict['dodispersioncalc']:
        print('splitting regressors by time lag for phase delay estimation')
        laglist = np.arange(optiondict['dispersioncalc_lower'], optiondict['dispersioncalc_upper'],
                            optiondict['dispersioncalc_step'])
        dispersioncalcout = np.zeros((np.shape(laglist)[0], inputshape[1]), dtype=rt_floattype)
        fftlen = int(inputshape[1] // 2)
        fftlen -= fftlen % 2
        dispersioncalcspecmag = np.zeros((np.shape(laglist)[0], fftlen), dtype=rt_floattype)
        dispersioncalcspecphase = np.zeros((np.shape(laglist)[0], fftlen), dtype=rt_floattype)
        for lagnum in range(0, np.shape(laglist)[0]):
            lower = laglist[lagnum] - optiondict['dispersioncalc_step'] / 2.0
            upper = laglist[lagnum] + optiondict['dispersioncalc_step'] / 2.0
            inlagrange = np.where(
                locationmask * ampmask * np.where(lower < lagtimes, np.int16(1), np.int16(0))
                * np.where(lagtimes < upper, np.int16(1), np.int16(0)))[0]
            print('    summing', np.shape(inlagrange)[0], 'regressors with lags from', lower, 'to', upper)
            if np.shape(inlagrange)[0] > 0:
                dispersioncalcout[lagnum, :] = tide.corrnormalize(np.mean(shiftedtcs[inlagrange], axis=0), False, True,
                    windowfunc=optiondict['windowfunc'])
                freqs, dispersioncalcspecmag[lagnum, :], dispersioncalcspecphase[lagnum, :] = tide.polarfft(
                    dispersioncalcout[lagnum, :],
                    1.0 / fmritr)
            inlagrange = None
        tide.writenpvecs(dispersioncalcout,
                         optiondict['outputname'] + '_dispersioncalcvecs_pass' + str(passnum) + '.txt')
        tide.writenpvecs(dispersioncalcspecmag,
                         optiondict['outputname'] + '_dispersioncalcspecmag_pass' + str(passnum) + '.txt')
        tide.writenpvecs(dispersioncalcspecphase,
                         optiondict['outputname'] + '_dispersioncalcspecphase_pass' + str(passnum) + '.txt')
        tide.writenpvecs(freqs, optiondict['outputname'] + '_dispersioncalcfreqs_pass' + str(passnum) + '.txt')

    if optiondict['estimatePCAdims']:
        pcacomponents = 'mle'
    else:
        pcacomponents = 1
    icacomponents = 1

    if optiondict['refinetype'] == 'ica':
        print('performing ica refinement')
        thefit = FastICA(n_components=icacomponents).fit(refinevoxels)  # Reconstruct signals
        print('Using first of ', len(thefit.components_), ' components')
        icadata = thefit.components_[0]
        filteredavg = tide.corrnormalize(theprefilter.apply(optiondict['fmrifreq'], averagedata), True, True)
        filteredica = tide.corrnormalize(theprefilter.apply(optiondict['fmrifreq'], icadata), True, True)
        thepxcorr = pearsonr(filteredavg, filteredica)[0]
        print('ica/avg correlation = ', thepxcorr)
        if thepxcorr > 0.0:
            outputdata = 1.0 * icadata
        else:
            outputdata = -1.0 * icadata
    elif optiondict['refinetype'] == 'pca':
        print('performing pca refinement')
        thefit = PCA(n_components=pcacomponents).fit(refinevoxels)
        print('Using first of ', len(thefit.components_), ' components')
        pcadata = thefit.components_[0]
        filteredavg = tide.corrnormalize(theprefilter.apply(optiondict['fmrifreq'], averagedata), True, True)
        filteredpca = tide.corrnormalize(theprefilter.apply(optiondict['fmrifreq'], pcadata), True, True)
        thepxcorr = pearsonr(filteredavg, filteredpca)[0]
        print('pca/avg correlation = ', thepxcorr)
        if thepxcorr > 0.0:
            outputdata = 1.0 * pcadata
        else:
            outputdata = -1.0 * pcadata
    elif optiondict['refinetype'] == 'weighted_average':
        print('performing weighted averaging refinement')
        outputdata = np.nan_to_num(averagedata / weightsum)
    else:
        print('performing unweighted averaging refinement')
        outputdata = averagedata

    if optiondict['cleanrefined']:
        thefit, R = tide.mlregress(averagediscard, averagedata)
        fitcoff = rt_floatset(thefit[0, 1])
        datatoremove = rt_floatset(fitcoff * averagediscard)
        outputdata -= datatoremove
    print()
    print(str(
        volumetotal) + ' voxels used for refinement:',
          '\n	', locationfails, ' locationfails',
          '\n	', ampfails, ' ampfails',
          '\n	', lagfails, ' lagfails',
          '\n	', sigmafails, ' sigmafails')

    if optiondict['psdfilter']:
        outputdata = tide.xfuncfilt(outputdata, snr)

    # garbage collect
    collected = gc.collect()
    print("Garbage collector: collected %d objects." % collected)

    return volumetotal, outputdata, maskarray


def procOneVoxelWiener(vox, lagtc, inittc):
    thefit, R = tide.mlregress(lagtc, inittc)
    fitcoff = rt_floatset(thefit[0, 1])
    datatoremove = rt_floatset(fitcoff * lagtc)
    return vox, rt_floatset(thefit[0, 0]), rt_floatset(R), rt_floatset(R * R), fitcoff, \
           rt_floatset(thefit[0, 1] / thefit[0, 0]), datatoremove, rt_floatset(inittc - datatoremove)


def wienerpass(numspatiallocs, reportstep, fmri_data, threshval, lagtc, optiondict, meanvalue, rvalue, r2value, fitcoff,
               fitNorm, datatoremove, filtereddata):
    if optiondict['multiproc']:
        # define the consumer function here so it inherits most of the arguments
        def Wiener_consumer(inQ, outQ):
            while True:
                try:
                    # get a new message
                    val = inQ.get()

                    # this is the 'TERM' signal
                    if val is None:
                        break

                    # process and send the data
                    outQ.put(procOneVoxelWiener(val, lagtc[val, :], fmri_data[val, optiondict['addedskip']:]))

                except Exception as e:
                    print("error!", e)
                    break

        # initialize the workers and the queues
        n_workers = optiondict['nprocs']
        inQ = mp.Queue()
        outQ = mp.Queue()
        workers = [mp.Process(target=Wiener_consumer, args=(inQ, outQ)) for i in range(n_workers)]
        for i, w in enumerate(workers):
            w.start()

        # pack the data and send to workers
        data_in = []
        for d in range(numspatiallocs):
            if np.mean(fmri_data[d, optiondict['addedskip']:]) >= threshval:
                data_in.append(d)
        print('processing', len(data_in), 'voxels with', n_workers, 'processes')
        data_out = process_data(data_in, inQ, outQ,  showprogressbar=optiondict['showprogressbar'],
                                chunksize=optiondict['mp_chunksize'])

        # shut down workers
        for i in range(n_workers):
            inQ.put(None)
        for w in workers:
            w.terminate()
            w.join()

        # unpack the data
        volumetotal = 0
        for voxel in data_out:
            meanvalue[voxel[0]] = voxel[1]
            rvalue[voxel[0]] = voxel[2]
            r2value[voxel[0]] = voxel[3]
            fitcoff[voxel[0]] = voxel[4]
            fitNorm[voxel[0]] = voxel[5]
            datatoremove[voxel[0], :] = voxel[6]
            filtereddata[voxel[0], :] = voxel[7]
            volumetotal += 1
        data_out = []
    else:
        volumetotal = 0
        for vox in range(0, numspatiallocs):
            if (vox % reportstep == 0 or vox == numspatiallocs - 1) and optiondict['showprogressbar']:
                tide.progressbar(vox + 1, numspatiallocs, label='Percent complete')
            inittc = fmri_data[vox, optiondict['addedskip']:].copy()
            if np.mean(inittc) >= threshval:
                dummy, meanvalue[vox], rvalue[vox], r2value[vox], fitcoff[vox], fitNorm[vox], datatoremove[vox], \
                filtereddata[vox] = procOneVoxelWiener(vox, lagtc[vox, :], inittc)
                volumetotal += 1

    return volumetotal


def procOneVoxelGLM(vox, lagtc, inittc):
    thefit, R = tide.mlregress(lagtc, inittc)
    fitcoff = rt_floatset(thefit[0, 1])
    datatoremove = rt_floatset(fitcoff * lagtc)
    return vox, rt_floatset(thefit[0, 0]), rt_floatset(R), rt_floatset(R * R), fitcoff, \
           rt_floatset(thefit[0, 1] / thefit[0, 0]), datatoremove, rt_floatset(inittc - datatoremove)


def glmpass(numspatiallocs, reportstep, fmri_data, threshval, lagtc, optiondict, meanvalue, rvalue, r2value, fitcoff,
            fitNorm, datatoremove, filtereddata):
    if optiondict['multiproc']:
        # define the consumer function here so it inherits most of the arguments
        def GLM_consumer(inQ, outQ):
            while True:
                try:
                    # get a new message
                    val = inQ.get()

                    # this is the 'TERM' signal
                    if val is None:
                        break

                    # process and send the data
                    outQ.put(procOneVoxelGLM(val, lagtc[val, :], fmri_data[val, optiondict['addedskip']:]))

                except Exception as e:
                    print("error!", e)
                    break

        # initialize the workers and the queues
        n_workers = optiondict['nprocs']
        inQ = mp.Queue()
        outQ = mp.Queue()
        workers = [mp.Process(target=GLM_consumer, args=(inQ, outQ)) for i in range(n_workers)]
        for i, w in enumerate(workers):
            w.start()

        # pack the data and send to workers
        data_in = []
        for d in range(numspatiallocs):
            if (np.mean(fmri_data[d, optiondict['addedskip']:]) >= threshval) or optiondict['nothresh']:
                data_in.append(d)
        print('processing', len(data_in), 'voxels with', n_workers, 'processes')
        data_out = process_data(data_in, inQ, outQ,  showprogressbar=optiondict['showprogressbar'],
                                chunksize=optiondict['mp_chunksize'])

        # shut down workers
        for i in range(n_workers):
            inQ.put(None)
        for w in workers:
            w.terminate()
            w.join()

        # unpack the data
        volumetotal = 0
        for voxel in data_out:
            meanvalue[voxel[0]] = voxel[1]
            rvalue[voxel[0]] = voxel[2]
            r2value[voxel[0]] = voxel[3]
            fitcoff[voxel[0]] = voxel[4]
            fitNorm[voxel[0]] = voxel[5]
            datatoremove[voxel[0], :] = voxel[6]
            filtereddata[voxel[0], :] = voxel[7]
            volumetotal += 1
        data_out = []
    else:
        volumetotal = 0
        for vox in range(0, numspatiallocs):
            if (vox % reportstep == 0 or vox == numspatiallocs - 1) and optiondict['showprogressbar']:
                tide.progressbar(vox + 1, numspatiallocs, label='Percent complete')
            inittc = fmri_data[vox, optiondict['addedskip']:].copy()
            if np.mean(inittc) >= threshval:
                dummy, meanvalue[vox], rvalue[vox], r2value[vox], fitcoff[vox], fitNorm[vox], datatoremove[vox], \
                filtereddata[vox] = \
                    procOneVoxelGLM(vox, lagtc[vox, :], inittc)
                volumetotal += 1
                # if optiondict['doprewhiten']:
                #    arcoffs[vox, :] = pacf_yw(thefilttc, nlags=optiondict['armodelorder'])[1:]
                #    prewhiteneddata[vox, :] = rt_floatset(prewhiten(inittc, arcoffs[vox, :]))

    return volumetotal


def maketmask(filename, timeaxis, maskvector):
    inputdata = tide.readvecs(filename)
    theshape = np.shape(inputdata)
    for idx in range(0, theshape[1]):
        starttime = inputdata[0, idx]
        endtime = starttime + inputdata[1, idx]
        startindex = np.max((bisect.bisect_left(timeaxis, starttime), 0))
        endindex = np.min((bisect.bisect_right(timeaxis, endtime), len(maskvector) - 1))
        maskvector[startindex:endindex] = 1.0
        print(starttime, startindex, endtime, endindex)
    if False:
        fig = figure()
        ax = fig.add_subplot(111)
        ax.set_title('temporal mask vector')
        plot(timeaxis, maskvector)
        show()
    return maskvector


def prewhiten(indata, arcoffs):
    pwdata = 1.0 * indata
    for i in range(0, len(arcoffs)):
        pwdata[(i + 1):] = pwdata[(i + 1):] + arcoffs[i] * indata[:(-1 - i)]
    return pwdata


def numpy2shared(inarray, thetype):
    thesize = inarray.size
    theshape = inarray.shape
    if thetype == np.float64:
        inarray_shared = mp.RawArray('d', inarray.reshape((thesize)))
    else:
        inarray_shared = mp.RawArray('f', inarray.reshape((thesize)))
    inarray = np.frombuffer(inarray_shared, dtype=thetype, count=thesize)
    inarray.shape = theshape
    return inarray, inarray_shared, theshape


def allocshared(theshape, thetype):
    thesize = int(1)
    for element in theshape:
        thesize *= int(element)
    if thetype == np.float64:
        outarray_shared = mp.RawArray('d', thesize)
    else:
        outarray_shared = mp.RawArray('f', thesize)
    outarray = np.frombuffer(outarray_shared, dtype=thetype, count=thesize)
    outarray.shape = theshape
    return outarray, outarray_shared, theshape


def logmem(msg, file=None):
    if msg is None:
        logline = ','.join([
            '',
            'Self Max RSS',
            'Self Shared Mem',
            'Self Unshared Mem',
            'Self Unshared Stack',
            'Self Non IO Page Fault'
            'Self IO Page Fault'
            'Self Swap Out',
            'Children Max RSS',
            'Children Shared Mem',
            'Children Unshared Mem',
            'Children Unshared Stack',
            'Children Non IO Page Fault'
            'Children IO Page Fault'
            'Children Swap Out'])
    else:
        rcusage = resource.getrusage(resource.RUSAGE_SELF)
        outvals = [msg]
        outvals.append(str(rcusage.ru_maxrss))
        outvals.append(str(rcusage.ru_ixrss))
        outvals.append(str(rcusage.ru_idrss))
        outvals.append(str(rcusage.ru_isrss))
        outvals.append(str(rcusage.ru_minflt))
        outvals.append(str(rcusage.ru_majflt))
        outvals.append(str(rcusage.ru_nswap))
        rcusage = resource.getrusage(resource.RUSAGE_CHILDREN)
        outvals.append(str(rcusage.ru_maxrss))
        outvals.append(str(rcusage.ru_ixrss))
        outvals.append(str(rcusage.ru_idrss))
        outvals.append(str(rcusage.ru_isrss))
        outvals.append(str(rcusage.ru_minflt))
        outvals.append(str(rcusage.ru_majflt))
        outvals.append(str(rcusage.ru_nswap))
        logline = ','.join(outvals)
    if file is None:
        print(logline)
    else:
        file.writelines(logline + "\n")


def getglobalsignal(indata, optiondict, includemask=None, excludemask=None):
    # mask to interesting voxels
    if optiondict['globalmaskmethod'] == 'mean':
        themask = tide.makemask(np.mean(indata, axis=1), optiondict['corrmaskthreshpct'])
    elif optiondict['globalmaskmethod'] == 'variance':
        themask = tide.makemask(np.var(indata, axis=1), optiondict['corrmaskthreshpct'])
    if optiondict['nothresh']:
        themask *= 0
        themask += 1
    if includemask is not None:
        themask = themask * includemask
    if excludemask is not None:
        themask = themask * excludemask

    # add up all the voxels
    globalmean = rt_floatset(indata[0, :])
    thesize = np.shape(themask)
    numvoxelsused = 0
    for vox in range(0, thesize[0]):
        if themask[vox] > 0.0:
            numvoxelsused += 1
            if optiondict['meanscaleglobal']:
                themean = np.mean(indata[vox, :])
                if themean != 0.0:
                    globalmean = globalmean + indata[vox, :] / themean - 1.0
            else:
                globalmean = globalmean + indata[vox, :]
    print()
    print('used ', numvoxelsused, ' voxels to calculate global mean signal')
    return tide.stdnormalize(globalmean)


def main():
    realtr = 0.0

def run():
    theprefilter = tide.noncausalfilter()
    theprefilter.setbutter(optiondict['usebutterworthfilter'], optiondict['filtorder'])

    # start the clock!
    timings = [['Start', time.time(), None, None]]
    print('rapidtide2 version:', optiondict['release_version'], optiondict['git_tag'])
    tide.checkimports(optiondict)

    # get the command line parameters
    filename = None
    inputfreq = None
    inputstarttime = None
    if len(sys.argv) < 3:
        usage()
        sys.exit()
    # handle required args first
    fmrifilename = sys.argv[1]
    outputname = sys.argv[2]
    optparsestart = 3

    # now scan for optional arguments
    try:
        opts, args = getopt.getopt(sys.argv[optparsestart:], 'abcdf:gh:i:mo:ps:r:t:vBCF:ILMN:O:PRSTVZ:', ['help',
                                                                                                          'nowindow',
                                                                                                          'windowfunc=',
                                                                                                          'datatstep=',
                                                                                                          'datafreq=',
                                                                                                          'lagminthresh=',
                                                                                                          'lagmaxthresh=',
                                                                                                          'ampthresh=',
                                                                                                          'dosighistfit',
                                                                                                          'sigmathresh=',
                                                                                                          'refineweighting=',
                                                                                                          'refineprenorm=',
                                                                                                          'corrmaskthresh=',
                                                                                                          'despecklepasses=',
                                                                                                          'despecklethresh=',
                                                                                                          'accheck',
                                                                                                          'acfix',
                                                                                                          'noprogressbar',
                                                                                                          'refinepasses=',
                                                                                                          'passes=',
                                                                                                          'corrmask=',
                                                                                                          'includemask=',
                                                                                                          'excludemask=',
                                                                                                          'refineoffset',
                                                                                                          'nofitfilt',
                                                                                                          'cleanrefined',
                                                                                                          'pca',
                                                                                                          'ica',
                                                                                                          'weightedavg',
                                                                                                          'avg',
                                                                                                          'psdfilter',
                                                                                                          'dispersioncalc',
                                                                                                          'noglm',
                                                                                                          'nosharedmem',
                                                                                                          'multiproc',
                                                                                                          'nprocs=',
                                                                                                          'debug',
                                                                                                          'nonumba',
                                                                                                          'tmask=',
                                                                                                          'nodetrend',
                                                                                                          'slicetimes=',
                                                                                                          'glmsourcefile=',
                                                                                                          'preservefiltering',
                                                                                                          'globalmaskmethod=',
                                                                                                          'numskip=',
                                                                                                          'nirs',
                                                                                                          'venousrefine',
                                                                                                          'nothresh',
                                                                                                          'limitoutput',
                                                                                                          'regressor=',
                                                                                                          'regressorfreq=',
                                                                                                          'regressortstep=',
                                                                                                          'regressorstart=',
                                                                                                          'timerange=',
                                                                                                          'refineupperlag',
                                                                                                          'refinelowerlag',
                                                                                                          'fastgauss',
                                                                                                          'memprofile',
                                                                                                          'nogaussrefine',
                                                                                                          'usesp',
                                                                                                          'liang',
                                                                                                          'eckart',
                                                                                                          'phat',
                                                                                                          'wiener',
                                                                                                          'weiner',
                                                                                                          'maxfittype=',
                                                                                                          'AR='])
    except getopt.GetoptError as err:
        # print help information and exit:
        print(str(err))  # will print something like 'option -a not recognized'
        usage()
        sys.exit(2)

    formattedcmdline = [sys.argv[0] + ' \\']
    for thearg in range(1, optparsestart):
        formattedcmdline.append('\t' + sys.argv[thearg] + ' \\')

    for o, a in opts:
        linkchar = ' '
        if o == '--nowindow':
            optiondict['usewindowfunc'] = False
            print('disable precorrelation windowing')
        elif o == '--windowfunc':
            thewindow = a
            if (thewindow != 'hamming') and (thewindow != 'hann') and (thewindow != 'blackmanharris') and (thewindow != 'None'):
                print('illegal window function', thewindow)
                sys.exit()
            optiondict['windowfunc'] = thewindow
            linkchar = '='
            print('Will use', optiondict['windowfunc'], 'as the window function for correlation')
        elif o == '-v':
            optiondict['verbose'] = True
            print('Turned on verbose mode')
        elif o == '--liang':
            optiondict['corrweighting'] = 'Liang'
            optiondict['dodetrend'] = True
            print('Enabled Liang weighted crosscorrelation')
        elif o == '--eckart':
            optiondict['corrweighting'] = 'Eckart'
            optiondict['dodetrend'] = True
            print('Enabled Eckart weighted crosscorrelation')
        elif o == '--phat':
            optiondict['corrweighting'] = 'PHAT'
            optiondict['dodetrend'] = True
            print('Enabled GCC-PHAT fitting')
        elif o == '--weiner':
            print('It\'s spelled wiener, not weiner')
            print('The filter is named after Norbert Wiener, an MIT mathemetician.  The name')
            print('probably indicates that his family came from Vienna.')
            print('Spell it right and try again.')
            sys.exit()
        elif o == '--cleanrefined':
            optiondict['cleanrefined'] = True
            optiondict['shiftall'] = True
            print('Will attempt to clean refined regressor')
        elif o == '--wiener':
            optiondict['dodeconv'] = True
            print('Will perform Wiener deconvolution')
        elif o == '--usesp':
            optiondict['internalprecision'] = 'single'
            print('Will use single precision for internal calculations')
        elif o == '--preservefiltering':
            optiondict['preservefiltering'] = True
            print('Will not reread input file prior to GLM')
        elif o == '--glmsourcefile':
            optiondict['glmsourcefile'] = a
            linkchar = '='
            print('Will regress delayed regressors out of', optiondict['glmsourcefile'])
        elif o == '--corrmaskthresh':
            optiondict['corrmaskthreshpct'] = float(a)
            linkchar = '='
            print('Will perform correlations in voxels where mean exceeds', optiondict['corrmaskthreshpct'],
                  '% of robust maximum')
        elif o == '-I':
            optiondict['invertregressor'] = True
            print('Invert the regressor prior to running')
        elif o == '-B':
            optiondict['bipolar'] = True
            print('Enabled bipolar correlation fitting')
        elif o == '-S':
            optiondict['fakerun'] = True
            print('report command line options and quit')
        elif o == '-a':
            optiondict['antialias'] = False
            print('antialiasing disabled')
        elif o == '-M':
            optiondict['useglobalref'] = True
            print('using global mean timecourse as the reference regressor')
        elif o == '--globalmaskmethod':
            optiondict['globalmaskmethod'] = a
            if optiondict['globalmaskmethod'] == 'mean':
                print('will use mean value to mask voxels prior to generating global mean')
            elif optiondict['globalmaskmethod'] == 'variance':
                print('will use timecourse variance to mask voxels prior to generating global mean')
            else:
                print(optiondict['globalmaskmethod'],
                      'is not a valid masking method.  Valid methods are \'mean\' and \'variance\'')
                sys.exit()
        elif o == '-m':
            optiondict['meanscaleglobal'] = True
            print('mean scale voxels prior to generating global mean')
        elif o == '--limitoutput':
            optiondict['savelagregressors'] = False
            print('disabling output of lagregressors and some ancillary GLM timecourses')
        elif o == '--debug':
            optiondict['debug'] = True
            theprefilter.setdebug(optiondict['debug'])
            print('enabling additional data output for debugging')
        elif o == '--multiproc':
            optiondict['multiproc'] = True
            optiondict['nprocs'] = -1
            print('enabling multiprocessing')
        elif o == '--nosharedmem':
            optiondict['sharedmem'] = False
            linkchar = '='
            print('will not use shared memory for large array storage')
        elif o == '--nprocs':
            optiondict['multiproc'] = True
            optiondict['nprocs'] = int(a)
            linkchar = '='
            print('will use', optiondict['nprocs'], 'processes for calculation')
        elif o == '--nonumba':
            optiondict['nonumba'] = True
            print('disabling numba if present')
        elif o == '--memprofile':
            if memprofilerexists:
                optiondict['memprofile'] = True
                print('enabling memory profiling')
            else:
                print('cannot enable memory profiling - memory_profiler module not found')
        elif o == '--noglm':
            optiondict['doglmfilt'] = False
            print('disabling GLM filter')
        elif o == '-T':
            optiondict['savecorrtimes'] = True
            print('saving a table of correlation times used')
        elif o == '-V':
            theprefilter.settype('vlf')
            print('prefiltering to vlf band')
        elif o == '-L':
            theprefilter.settype('lfo')
            optiondict['filtertype'] = 'lfo'
            optiondict['despeckle_thresh'] = np.max([optiondict['despeckle_thresh'], 0.5/(theprefilter.getfreqlimits()[2])])
            print('prefiltering to lfo band')
        elif o == '-R':
            theprefilter.settype('resp')
            optiondict['filtertype'] = 'resp'
            optiondict['despeckle_thresh'] = np.max([optiondict['despeckle_thresh'], 0.5/(theprefilter.getfreqlimits()[2])])
            print('prefiltering to respiratory band')
        elif o == '-C':
            theprefilter.settype('cardiac')
            optiondict['filtertype'] = 'cardiac'
            optiondict['despeckle_thresh'] = np.max([optiondict['despeckle_thresh'], 0.5/(theprefilter.getfreqlimits()[2])])
            print('prefiltering to cardiac band')
        elif o == '-F':
            arbvec = a.split(',')
            if len(arbvec) != 2 and len(arbvec) != 4:
                usage()
                sys.exit()
            if len(arbvec) == 2:
                optiondict['arb_lower'] = float(arbvec[0])
                optiondict['arb_upper'] = float(arbvec[1])
                optiondict['arb_lowerstop'] = 0.9 * float(arbvec[0])
                optiondict['arb_upperstop'] = 1.1 * float(arbvec[1])
            if len(arbvec) == 4:
                optiondict['arb_lower'] = float(arbvec[0])
                optiondict['arb_upper'] = float(arbvec[1])
                optiondict['arb_lowerstop'] = float(arbvec[2])
                optiondict['arb_upperstop'] = float(arbvec[3])
            theprefilter.settype('arb')
            optiondict['filtertype'] = 'arb'
            theprefilter.setarb(optiondict['arb_lowerstop'], optiondict['arb_lower'],
                                optiondict['arb_upper'], optiondict['arb_upperstop'])
            optiondict['despeckle_thresh'] = np.max([optiondict['despeckle_thresh'], 0.5/(theprefilter.getfreqlimits()[2])])
            print('prefiltering to ', optiondict['arb_lower'], optiondict['arb_upper'],
                  '(stops at ', optiondict['arb_lowerstop'], optiondict['arb_upperstop'], ')')
        elif o == '-p':
            optiondict['doprewhiten'] = True
            print('prewhitening data')
        elif o == '-P':
            optiondict['doprewhiten'] = True
            optiondict['saveprewhiten'] = True
            print('saving prewhitened data')
        elif o == '-d':
            optiondict['displayplots'] = True
            print('displaying all plots')
        elif o == '-N':
            optiondict['numestreps'] = int(a)
            if optiondict['numestreps'] == 0:
                optiondict['ampthreshfromsig'] = False
                print('Will not estimate significance thresholds from null correlations')
            else:
                print('Will estimate p<0.05 significance threshold from ', optiondict['numestreps'],
                      ' null correlations')
        elif o == '--accheck':
            optiondict['check_autocorrelation'] = True
            print('Will check for periodic components in the autocorrelation function')
        elif o == '--despecklethresh':
            if optiondict['despeckle_passes'] == 0:
                optiondict['despeckle_passes'] = 1
            optiondict['check_autocorrelation'] = True
            optiondict['despeckle_thresh'] = float(a)
            linkchar = '='
            print('Forcing despeckle threshhold to ', optiondict['despeckle_thresh'])
        elif o == '--despecklepasses':
            optiondict['check_autocorrelation'] = True
            optiondict['despeckle_passes'] = int(a)
            if optiondict['despeckle_passes'] < 1:
                print("minimum number of despeckle passes is 1")
                sys.exit()
            linkchar = '='
            print('Will do ', optiondict['despeckle_passes'], ' despeckling passes')
        elif o == '--acfix':
            optiondict['fix_autocorrelation'] = True
            optiondict['check_autocorrelation'] = True
            print('Will remove periodic components in the autocorrelation function (experimental)')
        elif o == '--noprogressbar':
            optiondict['showprogressbar'] = False
            print('Will disable progress bars')
        elif o == '-s':
            optiondict['widthlimit'] = float(a)
            print('Setting gaussian fit width limit to ', optiondict['widthlimit'], 'Hz')
        elif o == '-b':
            theprefilter.setbutter(True, optiondict['filtorder'])
            print('Using butterworth bandlimit filter')
        elif o == '-Z':
            optiondict['fixeddelayvalue'] = float(a)
            optiondict['fixdelay'] = True
            optiondict['lagmin'] = optiondict['fixeddelayvalue'] - 10.0
            optiondict['lagmax'] = optiondict['fixeddelayvalue'] + 10.0
            print('Delay will be set to ', optiondict['fixeddelayvalue'], 'in all voxels')
        elif o == '-f':
            optiondict['gausssigma'] = float(a)
            optiondict['dogaussianfilter'] = True
            print('Will prefilter fMRI data with a gaussian kernel of ', optiondict['gausssigma'], ' mm')
        elif o == '--timerange':
            limitvec = a.split(',')
            optiondict['startpoint'] = int(limitvec[0])
            optiondict['endpoint'] = int(limitvec[1])
            linkchar = '='
            print('Analysis will be performed only on data from point ', optiondict['startpoint'], ' to ',
                  optiondict['endpoint'])
        elif o == '-r':
            lagvec = a.split(',')
            if not optiondict['fixdelay']:
                optiondict['lagmin'] = float(lagvec[0])
                optiondict['lagmax'] = float(lagvec[1])
                print('Correlations will be calculated over range ', optiondict['lagmin'], ' to ', optiondict['lagmax'])
        elif o == '-y':
            optiondict['interptype'] = a
            if (optiondict['interptype'] != 'cubic') and (optiondict['interptype'] != 'quadratic') and (
                        optiondict['interptype'] != 'univariate'):
                print('unsupported interpolation type!')
                sys.exit()
        elif o == '-h':
            optiondict['histlen'] = int(a)
            print('Setting histogram length to ', optiondict['histlen'])
        elif o == '-o':
            optiondict['offsettime'] = float(a)
            optiondict['offsettime_total'] = -float(a)
            print('Applying a timeshift of ', optiondict['offsettime'], ' to regressor')
        elif o == '--datafreq':
            realtr = 1.0/float(a)
            linkchar = '='
            print('Data time step forced to ', realtr)
        elif o == '--datatstep':
            realtr = float(a)
            linkchar = '='
            print('Data time step forced to ', realtr)
        elif o == '-t':
            realtr = float(a)
            print('Data time step forced to ', realtr)
        elif o == '-c':
            optiondict['isgrayordinate'] = True
            print('Input fMRI file is a converted CIFTI file')
        elif o == '--AR':
            optiondict['armodelorder'] = int(a)
            if optiondict['armodelorder'] < 1:
                print('AR model order must be an integer greater than 0')
                sys.exit()
            linkchar = '='
            print('AR model order set to ', optiondict['armodelorder'])
        elif o == '-O':
            optiondict['oversampfactor'] = int(a)
            if optiondict['oversampfactor'] < 1:
                print('oversampling factor must be an integer greater than or equal to 1')
                sys.exit()
            print('oversampling factor set to ', optiondict['oversampfactor'])
        elif o == '--psdfilter':
            optiondict['psdfilter'] = True
            print('Will use a cross-spectral density filter on shifted timecourses prior to refinement')
        elif o == '--avg':
            optiondict['refinetype'] = 'unweighted_average'
            print('Will use unweighted average to refine regressor rather than simple averaging')
        elif o == '--weightedavg':
            optiondict['refinetype'] = 'weighted_average'
            print('Will use weighted average to refine regressor rather than simple averaging')
        elif o == '--ica':
            optiondict['refinetype'] = 'ica'
            print('Will use ICA procedure to refine regressor rather than simple averaging')
        elif o == '--dispersioncalc':
            optiondict['dodispersioncalc'] = True
            print('Will do dispersion calculation during regressor refinement')
        elif o == '--nofitfilt':
            optiondict['zerooutbadfit'] = False
            optiondict['nohistzero'] = True
            print('Correlation parameters will be recorded even if out of bounds')
        elif o == '--pca':
            optiondict['refinetype'] = 'pca'
            print('Will use PCA procedure to refine regressor rather than simple averaging')
        elif o == '--numskip':
            optiondict['preprocskip'] = int(a)
            linkchar = '='
            print('Setting preprocessing trs skipped to ', optiondict['preprocskip'])
        elif o == '--venousrefine':
            optiondict['lagmaskside'] = 'upper'
            optiondict['lagminthresh'] = 2.5
            optiondict['lagmaxthresh'] = 6.0
            optiondict['ampthresh'] = 0.5
            print('Biasing refinement to voxels in draining vasculature')
        elif o == '--nirs':
            optiondict['nothresh'] = True
            optiondict['corrmaskthreshpct'] = 0.0
            optiondict['preservefiltering'] = True
            optiondict['refineprenorm'] = 'var'
            optiondict['ampthresh'] = 0.7
            optiondict['lagminthresh'] = 0.1
            print('Setting NIRS mode')
        elif o == '--nothresh':
            optiondict['nothresh'] = True
            optiondict['corrmaskthreshpct'] = 0.0
            print('Disabling voxel threshhold')
        elif o == '--regressor':
            filename = a
            optiondict['useglobalref'] = False
            linkchar = '='
            print('Will use regressor file', a)
        elif o == '--regressorfreq':
            inputfreq = float(a)
            linkchar = '='
            print('Setting regressor sample frequency to ', inputfreq)
        elif o == '--regressortstep':
            inputfreq = 1.0 / float(a)
            linkchar = '='
            print('Setting regressor sample time step to ', float(a))
        elif o == '--regressorstart':
            inputstarttime = float(a)
            linkchar = '='
            print('Setting regressor start time to ', inputstarttime)
        elif o == '--slicetimes':
            optiondict['slicetimes'] = tide.readvecs(a)
            linkchar = '='
            print('Using slicetimes from file', a)
        elif o == '--nodetrend':
            optiondict['dodetrend'] = False
            print('Disabling linear trend removal in regressor generation and correlation preparation')
        elif o == '--refineupperlag':
            optiondict['lagmaskside'] = 'upper'
            print('Will only use lags between ', optiondict['lagminthresh'], ' and ', optiondict['lagmaxthresh'],
                  ' in refinement')
        elif o == '--refinelowerlag':
            optiondict['lagmaskside'] = 'lower'
            print('Will only use lags between ', -optiondict['lagminthresh'], ' and ', -optiondict['lagmaxthresh'],
                  ' in refinement')
        elif o == '--nogaussrefine':
            optiondict['gaussrefine'] = False
            print('Will not use gaussian correlation peak refinement')
        elif o == '--fastgauss':
            optiondict['fastgauss'] = True
            print('Will use alternative fast gauss refinement (does not work well)')
        elif o == '--refineoffset':
            optiondict['refineoffset'] = True
            if optiondict['passes'] == 1:
                optiondict['passes'] = 2
            print('Will refine offset time during subsequent passes')
        elif o == '--lagminthresh':
            optiondict['lagminthresh'] = float(a)
            if optiondict['passes'] == 1:
                optiondict['passes'] = 2
            linkchar = '='
            print('Using lagminthresh of ', optiondict['lagminthresh'])
        elif o == '--lagmaxthresh':
            optiondict['lagmaxthresh'] = float(a)
            if optiondict['passes'] == 1:
                optiondict['passes'] = 2
            linkchar = '='
            print('Using lagmaxthresh of ', optiondict['lagmaxthresh'])
        elif o == '--skipsighistfit':
            optiondict['dosighistfit'] = False
            print('will not fit significance histogram with a Johnson SB function')
        elif o == '--ampthresh':
            optiondict['ampthresh'] = float(a)
            optiondict['ampthreshfromsig'] = False
            if optiondict['passes'] == 1:
                optiondict['passes'] = 2
            linkchar = '='
            print('Using ampthresh of ', optiondict['ampthresh'])
        elif o == '--sigmathresh':
            optiondict['sigmathresh'] = float(a)
            if optiondict['passes'] == 1:
                optiondict['passes'] = 2
            linkchar = '='
            print('Using widththresh of ', optiondict['sigmathresh'])
        elif o == '--excludemask':
            optiondict['excludemaskname'] = a
            linkchar = '='
            print('Voxels in ', optiondict['excludemaskname'], ' will not be used to define or refine regressors')
        elif o == '--corrmask':
            optiondict['corrmaskname'] = a
            linkchar = '='
            print('Using ', optiondict['corrmaskname'], ' as mask file - corrmaskthresh will be ignored')
        elif o == '--includemask':
            optiondict['includemaskname'] = a
            linkchar = '='
            print('Only voxels in ', optiondict['includemaskname'], ' will be used to define or refine regressors')
        elif o == '--refineprenorm':
            optiondict['refineprenorm'] = a
            if (
                        optiondict['refineprenorm'] != 'None') and (
                        optiondict['refineprenorm'] != 'mean') and (
                        optiondict['refineprenorm'] != 'var') and (
                        optiondict['refineprenorm'] != 'std') and (
                        optiondict['refineprenorm'] != 'invlag'):
                print('unsupported refinement prenormalization mode!')
                sys.exit()
            linkchar = '='
        elif o == '--refineweighting':
            optiondict['refineweighting'] = a
            if (
                        optiondict['refineweighting'] != 'None') and (
                        optiondict['refineweighting'] != 'NIRS') and (
                        optiondict['refineweighting'] != 'R') and (
                        optiondict['refineweighting'] != 'R2'):
                print('unsupported refinement weighting!')
                sys.exit()
            linkchar = '='
        elif o == '--tmask':
            optiondict['usetmask'] = True
            optiondict['tmaskname'] = a
            linkchar = '='
            print('Will multiply regressor by timecourse in ', optiondict['tmaskname'])
        elif o == '--refinepasses' or o == '--passes':
            if o == '--refinepasses':
                print('WARNING - refinepasses is depracated - use passes instead')
            optiondict['passes'] = int(a)
            linkchar = '='
            print('Will do ', optiondict['passes'], ' processing passes')
        elif o == '--maxfittype':
            optiondict['findmaxtype'] = a
            linkchar = '='
            print('Will do ', optiondict['findmaxtype'], ' peak fitting')
        elif o in ('-h', '--help'):
            usage()
            sys.exit()
        else:
            assert False, 'unhandled option'
        formattedcmdline.append('\t' + o + linkchar + a + ' \\')
    formattedcmdline[len(formattedcmdline) - 1] = formattedcmdline[len(formattedcmdline) - 1][:-2]

    optiondict['dispersioncalc_lower'] = optiondict['lagmin']
    optiondict['dispersioncalc_upper'] = optiondict['lagmax']
    optiondict['dispersioncalc_step'] = np.max(
        [(optiondict['dispersioncalc_upper'] - optiondict['dispersioncalc_lower']) / 25,
         optiondict['dispersioncalc_step']])
    timings.append(['Argument parsing done', time.time(), None, None])

    # don't use shared memory if there is only one process
    if not optiondict['multiproc']:
        optiondict['sharedmem'] = False
        print('running single process - disabled shared memory use')

    # disable numba now if we're going to do it (before any jits)
    if optiondict['nonumba']:
        tide.disablenumba()

    # set the internal precision
    global rt_floatset, rt_floattype
    if optiondict['internalprecision'] == 'double':
        print('setting internal precision to double')
        rt_floattype = 'float64'
        rt_floatset = np.float64
    else:
        print('setting internal precision to single')
        rt_floattype = 'float32'
        rt_floatset = np.float32

    # set the output precision
    if optiondict['outputprecision'] == 'double':
        print('setting output precision to double')
        rt_outfloattype = 'float64'
        rt_outfloatset = np.float64
    else:
        print('setting output precision to single')
        rt_outfloattype = 'float32'
        rt_outfloatset = np.float32

    # set set the number of worker processes if multiprocessing
    if optiondict['nprocs'] < 1:
        optiondict['nprocs'] = mp.cpu_count() - 1

    # write out the command used
    tide.writevec(formattedcmdline, outputname + '_formattedcommandline.txt')
    tide.writevec([' '.join(sys.argv)], outputname + '_commandline.txt')

    # add additional information to option structure for debugging
    optiondict['fmrifilename'] = fmrifilename
    optiondict['outputname'] = outputname
    optiondict['regressorfile'] = filename

    # open up the memory usage file
    if not optiondict['memprofile']:
        memfile = open(outputname + '_memusage.csv', 'w')
        logmem(None, file=memfile)

    # open the fmri datafile
    logmem('before reading in fmri data', file=memfile)
    if tide.checkiftext(fmrifilename):
        print('input file is text - all I/O will be to text files')
        optiondict['textio'] = True
        if optiondict['dogaussianfilter']:
            optiondict['dogaussianfilter'] = False
            print('gaussian spatial filter disabled for text input files')

    if optiondict['textio']:
        nim_data = tide.readvecs(fmrifilename)
        theshape = np.shape(nim_data)
        xsize = theshape[0]
        ysize = 1
        numslices = 1
        fileiscifti = False
        timepoints = theshape[1]
        thesizes = [0, int(xsize), 1, 1, int(timepoints)]
        numspatiallocs = int(xsize)
        slicesize = numspatiallocs
    else:
        nim, nim_data, nim_hdr, thedims, thesizes = tide.readfromnifti(fmrifilename)
        if nim_hdr['intent_code'] == 3002:
            print('input file is CIFTI')
            optiondict['isgrayordinate'] = True
            fileiscifti = True
            timepoints = nim_data.shape[4]
            numspatiallocs = nim_data.shape[5]
            slicesize = numspatiallocs
        else:
            print('input file is NIFTI')
            fileiscifti = False
            xsize, ysize, numslices, timepoints = tide.parseniftidims(thedims)
            numspatiallocs = int(xsize) * int(ysize) * int(numslices)
            slicesize = numspatiallocs / int(numslices)
        xdim, ydim, slicethickness, tr = tide.parseniftisizes(thesizes)
    logmem('after reading in fmri data', file=memfile)

    # correct some fields if necessary
    if optiondict['isgrayordinate']:
        fmritr = 0.72  # this is wrong and is a hack until I can parse CIFTI XML
    else:
        if optiondict['textio']:
            if realtr <= 0.0:
                print('for text file data input, you must use the -t option to set the timestep')
                sys.exit()
        else:
            if nim_hdr.get_xyzt_units()[1] == 'msec':
                fmritr = thesizes[4] / 1000.0
            else:
                fmritr = thesizes[4]
    if realtr > 0.0:
        fmritr = realtr
    oversamptr = fmritr / optiondict['oversampfactor']
    if optiondict['verbose']:
        print('fmri data: ', timepoints, ' timepoints, tr = ', fmritr, ', oversamptr =', oversamptr)
    print(numspatiallocs, ' spatial locations, ', timepoints, ' timepoints')
    timings.append(['Finish reading fmrifile', time.time(), None, None])

    # if the user has specified start and stop points, limit check, then use these numbers
    validstart, validend = startendcheck(timepoints, optiondict['startpoint'], optiondict['endpoint'])
    if abs(optiondict['lagmin']) > (validend - validstart + 1) * fmritr / 2.0:
        print('magnitude of lagmin exceeds', (validend - validstart + 1) * fmritr / 2.0, ' - invalid')
        sys.exit()
    if abs(optiondict['lagmax']) > (validend - validstart + 1) * fmritr / 2.0:
        print('magnitude of lagmax exceeds', (validend - validstart + 1) * fmritr / 2.0, ' - invalid')
        sys.exit()
    if optiondict['dogaussianfilter']:
        print('applying gaussian spatial filter to timepoints ', validstart, ' to ', validend)
        reportstep = 10
        for i in range(validstart, validend + 1):
            if (i % reportstep == 0 or i == validend) and optiondict['showprogressbar']:
                tide.progressbar(i - validstart + 1, timepoints, label='Percent complete')
            nim_data[:, :, :, i] = tide.ssmooth(xdim, ydim, slicethickness, optiondict['gausssigma'],
                                                nim_data[:, :, :, i])
        timings.append(['End 3D smoothing', time.time(), None, None])
        print()

    # reshape the data and trim to a time range, if specified.  Check for special case of no trimming to save RAM
    if (validstart == 0) and (validend == timepoints):
        fmri_data = nim_data.reshape((numspatiallocs, timepoints))
    else:
        fmri_data = nim_data.reshape((numspatiallocs, timepoints))[:, validstart:validend + 1]
        timepoints = validend - validstart + 1

    # read in the optional masks
    logmem('before setting masks', file=memfile)
    internalincludemask = None
    internalexcludemask = None
    if optiondict['includemaskname'] is not None:
        if optiondict['textio']:
            theincludemask = tide.readvecs(optiondict['includemaskname']).astype('int16')
            theshape = np.shape(nim_data)
            theincludexsize = theshape[0]
            if not theincludexsize == xsize:
                print('Dimensions of include mask do not match the fmri data - exiting')
                sys.exit()
        else:
            nimincludemask, theincludemask, nimincludemask_hdr, theincludemaskdims, theincludmasksizes = tide.readfromnifti(
                optiondict['includemaskname'])
            if not tide.checkspacematch(theincludemaskdims, thedims):
                print('Dimensions of include mask do not match the fmri data - exiting')
                sys.exit()
        internalincludemask = theincludemask.reshape(numspatiallocs)
    if optiondict['excludemaskname'] is not None:
        if optiondict['textio']:
            theexcludemask = tide.readvecs(optiondict['excludemaskname']).astype('int16')
            theexcludemask = 1.0 - theexcludemask
            theshape = np.shape(nim_data)
            theexcludexsize = theshape[0]
            if not theexcludexsize == xsize:
                print('Dimensions of exclude mask do not match the fmri data - exiting')
                sys.exit()
        else:
            nimexcludemask, theexcludemask, nimexcludemask_hdr, theexcludemaskdims, theexcludmasksizes = tide.readfromnifti(
                optiondict['excludemaskname'])
            theexcludemask = 1.0 - theexcludemask
            if not tide.checkspacematch(theexcludemaskdims, thedims):
                print('Dimensions of exclude mask do not match the fmri data - exiting')
                sys.exit()
        internalexcludemask = theexcludemask.reshape(numspatiallocs)
    logmem('after setting masks', file=memfile)

    # find the threshold value for the image data
    logmem('before selecting valid voxels', file=memfile)
    threshval = tide.getfracval(fmri_data[:, optiondict['addedskip']], 0.98) / 25.0
    if optiondict['corrmaskname'] is not None:
        if optiondict['textio']:
            corrmask = tide.readvecs(optiondict['corrmaskname']).astype('int16')
            theshape = np.shape(nim_data)
            corrxsize = theshape[0]
            if not corrxsize == xsize:
                print('Dimensions of correlation mask do not match the fmri data - exiting')
                sys.exit()
        else:
            nimcorrmask, nimcorrmask, nimcorrmask_hdr, corrmaskdims, theincludmasksizes = tide.readfromnifti(
                optiondict['corrmaskname'])
            if not tide.checkspacematch(corrmaskdims, thedims):
                print('Dimensions of correlation mask do not match the fmri data - exiting')
                sys.exit()
            corrmask = np.uint16(nimcorrmask.reshape((numspatiallocs)))
    else:
        corrmask = np.uint16(tide.makemask(np.mean(fmri_data[:, optiondict['addedskip']:], axis=1),
                                       threshpct=optiondict['corrmaskthreshpct']))

    if optiondict['nothresh']:
        corrmask *= 0
        corrmask += 1
        threshval = -10000000.0
    if optiondict['verbose']:
        print('image threshval =', threshval)
    validvoxels = np.where(corrmask > 0)[0]
    numvalidspatiallocs = np.shape(validvoxels)[0]
    print('validvoxels shape =', numvalidspatiallocs)
    fmri_data_valid = fmri_data[validvoxels, :] + 0.0
    print('original size =', np.shape(fmri_data), ', trimmed size =', np.shape(fmri_data_valid))
    if internalincludemask is not None:
        internalincludemask_valid = 1.0 * internalincludemask[validvoxels]
        internalincludemask = None
        print('internalincludemask_valid has size:', internalincludemask_valid.size)
    else:
        internalincludemask_valid = None
    if internalexcludemask is not None:
        internalexcludemask_valid = 1.0 * internalexcludemask[validvoxels]
        internalexcludemask = None
        print('internalexcludemask_valid has size:', internalexcludemask_valid.size)
    else:
        internalexcludemask_valid = None
    logmem('after selecting valid voxels', file=memfile)

    # move fmri_data_valid into shared memory
    if optiondict['sharedmem']:
        print('moving fmri data to shared memory')
        timings.append(['Start moving fmri_data to shared memory', time.time(), None, None])
        if optiondict['memprofile']:
            numpy2shared_func = profile(numpy2shared, precision=2)
        else:
            logmem('before fmri data move', file=memfile)
            numpy2shared_func = numpy2shared
        fmri_data_valid, fmri_data_valid_shared, fmri_data_valid_shared_shape = numpy2shared_func(fmri_data_valid, rt_floatset)
        timings.append(['End moving fmri_data to shared memory', time.time(), None, None])

    # get rid of memory we aren't using
    logmem('before purging full sized fmri data', file=memfile)
    fmri_data = None
    nim_data = None
    logmem('after purging full sized fmri data', file=memfile)

    # read in the timecourse to resample
    timings.append(['Start of reference prep', time.time(), None, None])
    if filename is None:
        print('no regressor file specified - will use the global mean regressor')
        optiondict['useglobalref'] = True

    if optiondict['useglobalref']:
        inputfreq = 1.0 / fmritr
        inputperiod = 1.0 * fmritr
        inputstarttime = 0.0
        inputvec = getglobalsignal(fmri_data_valid, optiondict, includemask=internalincludemask_valid,
                                   excludemask=internalexcludemask_valid)
        optiondict['preprocskip'] = 0
    else:
        if inputfreq is None:
            print('no regressor frequency specified - defaulting to 1/tr')
            inputfreq = 1.0 / fmritr
        if inputstarttime is None:
            print('no regressor start time specified - defaulting to 0.0')
            inputstarttime = 0.0
        inputperiod = 1.0 / inputfreq
        inputvec = tide.readvec(filename)
    numreference = len(inputvec)
    optiondict['inputfreq'] = inputfreq
    optiondict['inputstarttime'] = inputstarttime
    print('regressor start time, end time, and step', inputstarttime, inputstarttime + numreference * inputperiod,
          inputperiod)

    if optiondict['verbose']:
        print('input vector length', len(inputvec), 'input freq', inputfreq, 'input start time', inputstarttime)

    reference_x = np.arange(0.0, numreference) * inputperiod - (inputstarttime + optiondict['offsettime'])

    # Print out initial information
    if optiondict['verbose']:
        print('there are ', numreference, ' points in the original regressor')
        print('the timepoint spacing is ', 1.0 / inputfreq)
        print('the input timecourse start time is ', inputstarttime)

    # generate the time axes
    fmrifreq = 1.0 / fmritr
    optiondict['fmrifreq'] = fmrifreq
    skiptime = fmritr * (optiondict['preprocskip'] + optiondict['addedskip'])
    print('first fMRI point is at ', skiptime, ' seconds relative to time origin')
    initial_fmri_x = np.arange(0.0, timepoints - optiondict['addedskip']) * fmritr + skiptime
    os_fmri_x = np.arange(0.0, (timepoints - optiondict['addedskip']) * optiondict['oversampfactor'] - (
        optiondict['oversampfactor'] - 1)) * oversamptr + skiptime

    if optiondict['verbose']:
        print(np.shape(os_fmri_x)[0])
        print(np.shape(initial_fmri_x)[0])

    # Clip the data
    if not optiondict['useglobalref'] and False:
        clipstart = bisect.bisect_left(reference_x, os_fmri_x[0] - 2.0 * optiondict['lagmin'])
        clipend = bisect.bisect_left(reference_x, os_fmri_x[-1] + 2.0 * optiondict['lagmax'])
        print('clip indices=', clipstart, clipend, reference_x[clipstart], reference_x[clipend], os_fmri_x[0],
              os_fmri_x[-1])

    # generate the comparison regressor from the input timecourse
    # correct the output time points
    # check for extrapolation
    if os_fmri_x[0] < reference_x[0]:
        print('WARNING: extrapolating ', os_fmri_x[0] - reference_x[0], ' seconds of data at beginning of timecourse')
    if os_fmri_x[-1] > reference_x[-1]:
        print('WARNING: extrapolating ', os_fmri_x[-1] - reference_x[-1], ' seconds of data at end of timecourse')

    # invert the regressor if necessary
    if optiondict['invertregressor']:
        invertfac = -1.0
    else:
        invertfac = 1.0

    # detrend the regressor if necessary
    if optiondict['dodetrend']:
        reference_y = invertfac * tide.detrend(inputvec[0:numreference], demean=optiondict['dodemean'])
    else:
        reference_y = invertfac * (inputvec[0:numreference] - np.mean(inputvec[0:numreference]))

    # write out the reference regressor prior to filtering
    tide.writenpvecs(reference_y, outputname + '_reference_origres_prefilt.txt')

    # band limit the regressor if that is needed
    print('filtering to ', theprefilter.gettype(), ' band')
    reference_y_classfilter = theprefilter.apply(inputfreq, reference_y)
    reference_y = reference_y_classfilter

    # write out the reference regressor used
    tide.writenpvecs(tide.stdnormalize(reference_y), outputname + '_reference_origres.txt')

    # filter the input data for antialiasing
    if optiondict['antialias']:
        if optiondict['zpfilter']:
            print('applying zero phase antialiasing filter')
            if optiondict['verbose']:
                print('    input freq:', inputfreq)
                print('    fmri freq:', fmrifreq)
                print('    npoints:', np.shape(reference_y)[0])
                print('    filtorder:', optiondict['filtorder'])
            reference_y_filt = tide.dolpfiltfilt(inputfreq, 0.5 * fmrifreq, reference_y, optiondict['filtorder'],
                                                 padlen=int(inputfreq * optiondict['padseconds']), debug=optiondict['debug'])
        else:
            if optiondict['trapezoidalfftfilter']:
                print('applying trapezoidal antialiasing filter')
                reference_y_filt = tide.dolptrapfftfilt(inputfreq, 0.25 * fmrifreq, 0.5 * fmrifreq, reference_y,
                                                        padlen=int(inputfreq * optiondict['padseconds']), debug=optiondict['debug'])
            else:
                print('applying brickwall antialiasing filter')
                reference_y_filt = tide.dolpfftfilt(inputfreq, 0.5 * fmrifreq, reference_y,
                                                    padlen=int(inputfreq * optiondict['padseconds']), debug=optiondict['debug'])
        reference_y = rt_floatset(reference_y_filt.real)

    warnings.filterwarnings('ignore', 'Casting*')

    if optiondict['fakerun']:
        sys.exit()

    # write out the resampled reference regressors
    if optiondict['dodetrend']:
        resampnonosref_y = tide.detrend(
            tide.doresample(reference_x, reference_y, initial_fmri_x, method=optiondict['interptype']),
            demean=optiondict['dodemean'])
        resampref_y = tide.detrend(
            tide.doresample(reference_x, reference_y, os_fmri_x, method=optiondict['interptype']),
            demean=optiondict['dodemean'])
    else:
        resampnonosref_y = tide.doresample(reference_x, reference_y, initial_fmri_x, method=optiondict['interptype'])
        resampref_y = tide.doresample(reference_x, reference_y, os_fmri_x, method=optiondict['interptype'])

    # prepare the temporal mask
    if optiondict['usetmask']:
        tmask_y = maketmask(optiondict['tmaskname'], reference_x, rt_floatset(reference_y))
        tmaskos_y = tide.doresample(reference_x, tmask_y, os_fmri_x, method=optiondict['interptype'])
        tide.writenpvecs(tmask_y, outputname + '_temporalmask.txt')

    if optiondict['usetmask']:
        resampnonosref_y *= tmask_y
        thefit, R = tide.mlregress(tmask_y, resampnonosref_y)
        resampnonosref_y -= thefit[0, 1] * tmask_y
        resampref_y *= tmaskos_y
        thefit, R = tide.mlregress(tmaskos_y, resampref_y)
        resampref_y -= thefit[0, 1] * tmaskos_y

    if optiondict['passes'] > 1:
        nonosrefname = '_reference_fmrires_pass1.txt'
        osrefname = '_reference_resampres_pass1.txt'
    else:
        nonosrefname = '_reference_fmrires.txt'
        osrefname = '_reference_resampres.txt'

    tide.writenpvecs(tide.stdnormalize(resampnonosref_y), outputname + nonosrefname)
    tide.writenpvecs(tide.stdnormalize(resampref_y), outputname + osrefname)
    timings.append(['End of reference prep', time.time(), None, None])

    corrtr = oversamptr
    if optiondict['verbose']:
        print('corrtr=', corrtr)

    numccorrlags = 2 * optiondict['oversampfactor'] * (timepoints - optiondict['addedskip']) - 1
    corrscale = np.arange(0.0, numccorrlags) * corrtr - (numccorrlags * corrtr) / 2.0 + (optiondict[
                                                                                             'oversampfactor'] - 0.5) * corrtr
    corrorigin = numccorrlags // 2 + 1
    lagmininpts = int((-optiondict['lagmin'] / corrtr) - 0.5)
    lagmaxinpts = int((optiondict['lagmax'] / corrtr) + 0.5)
    if optiondict['verbose']:
        print('corrorigin at point ', corrorigin, corrscale[corrorigin])
        print('corr range from ', corrorigin - lagmininpts, '(', corrscale[
            corrorigin - lagmininpts], ') to ', corrorigin + lagmaxinpts, '(', corrscale[corrorigin + lagmaxinpts], ')')

    if optiondict['savecorrtimes']:
        tide.writenpvecs(corrscale[corrorigin - lagmininpts:corrorigin + lagmaxinpts], outputname + '_corrtimes.txt')

    # allocate all of the data arrays
    logmem('before main array allocation', file=memfile)
    if optiondict['textio']:
        nativespaceshape = xsize
        nativearmodelshape = (xsize, optiondict['armodelorder'])
    else:
        if fileiscifti:
            nativespaceshape = (1, 1, 1, 1, numspatiallocs)
            nativearmodelshape = (1, 1, 1, optiondict['armodelorder'], numspatiallocs)
        else:
            nativespaceshape = (xsize, ysize, numslices)
            nativearmodelshape = (xsize, ysize, numslices, optiondict['armodelorder'])
    internalspaceshape = numspatiallocs
    internalarmodelshape = (numspatiallocs, optiondict['armodelorder'])
    internalvalidspaceshape = numvalidspatiallocs
    internalvalidarmodelshape = (numvalidspatiallocs, optiondict['armodelorder'])
    meanval = np.zeros(internalvalidspaceshape, dtype=rt_floattype)
    lagtimes = np.zeros(internalvalidspaceshape, dtype=rt_floattype)
    lagstrengths = np.zeros(internalvalidspaceshape, dtype=rt_floattype)
    lagsigma = np.zeros(internalvalidspaceshape, dtype=rt_floattype)
    lagmask = np.zeros(internalvalidspaceshape, dtype='uint16')
    R2 = np.zeros(internalvalidspaceshape, dtype=rt_floattype)
    outmaparray = np.zeros(internalspaceshape, dtype=rt_floattype)
    outarmodelarray = np.zeros(internalarmodelshape, dtype=rt_floattype)
    logmem('after main array allocation', file=memfile)

    corroutlen = np.shape(corrscale[corrorigin - lagmininpts:corrorigin + lagmaxinpts])[0]
    if optiondict['textio']:
        nativecorrshape = (xsize, corroutlen)
    else:
        if fileiscifti:
            nativecorrshape = (1, 1, 1, corroutlen, numspatiallocs)
        else:
            nativecorrshape = (xsize, ysize, numslices, corroutlen)
    internalcorrshape = (numspatiallocs, corroutlen)
    internalvalidcorrshape = (numvalidspatiallocs, corroutlen)
    print('allocating memory for correlation arrays', internalcorrshape, internalvalidcorrshape)
    if optiondict['sharedmem']:
        corrout, dummy, dummy = allocshared(internalvalidcorrshape, rt_floatset)
        gaussout, dummy, dummy = allocshared(internalvalidcorrshape, rt_floatset)
        outcorrarray, dummy, dummy = allocshared(internalcorrshape, rt_floatset)
    else:
        corrout = np.zeros(internalvalidcorrshape, dtype=rt_floattype)
        gaussout = np.zeros(internalvalidcorrshape, dtype=rt_floattype)
        outcorrarray = np.zeros(internalcorrshape, dtype=rt_floattype)
    logmem('after correlation array allocation', file=memfile)

    if optiondict['textio']:
        nativefmrishape = (xsize, np.shape(initial_fmri_x)[0])
    else:
        if fileiscifti:
            nativefmrishape = (1, 1, 1, np.shape(initial_fmri_x)[0], numspatiallocs)
        else:
            nativefmrishape = (xsize, ysize, numslices, np.shape(initial_fmri_x)[0])
    internalfmrishape = (numspatiallocs, np.shape(initial_fmri_x)[0])
    internalvalidfmrishape = (numvalidspatiallocs, np.shape(initial_fmri_x)[0])
    lagtc = np.zeros(internalvalidfmrishape, dtype=rt_floattype)
    logmem('after lagtc array allocation', file=memfile)

    if optiondict['passes'] > 1:
        if optiondict['sharedmem']:
            shiftedtcs, dummy, dummy = allocshared(internalvalidfmrishape, rt_floatset)
            weights, dummy, dummy = allocshared(internalvalidfmrishape, rt_floatset)
        else:
            shiftedtcs = np.zeros(internalvalidfmrishape, dtype=rt_floattype)
            weights = np.zeros(internalvalidfmrishape, dtype=rt_floattype)
        #refinemask = np.zeros(internalvalidspaceshape, dtype='uint16')
        logmem('after refinement array allocation', file=memfile)
    if optiondict['sharedmem']:
        outfmriarray, dummy, dummy = allocshared(internalfmrishape, rt_floatset)
    else:
        outfmriarray = np.zeros(internalfmrishape, dtype=rt_floattype)

    # prepare for fast resampling
    padvalue = max((-optiondict['lagmin'], optiondict['lagmax'])) + 30.0
    # print('setting up fast resampling with padvalue =',padvalue)
    numpadtrs = int(padvalue // fmritr)
    padvalue = fmritr * numpadtrs
    genlagtc = tide.fastresampler(reference_x, reference_y, padvalue=padvalue)

    # cycle over all voxels
    refine = True
    if optiondict['verbose']:
        print('refine is set to ', refine)
    optiondict['edgebufferfrac'] = max([optiondict['edgebufferfrac'], 2.0 / np.shape(corrscale)[0]])
    if optiondict['verbose']:
        print('edgebufferfrac set to ', optiondict['edgebufferfrac'])

    fft_fmri_data = None
    for thepass in range(1, optiondict['passes'] + 1):
        # initialize the pass
        if optiondict['passes'] > 1:
            print('\n\n*********************')
            print('Pass number ', thepass)

        referencetc = tide.corrnormalize(resampref_y, optiondict['usewindowfunc'], optiondict['dodetrend'], windowfunc=optiondict['windowfunc'])
        nonosreferencetc = tide.corrnormalize(resampnonosref_y, optiondict['usewindowfunc'], optiondict['dodetrend'], windowfunc=optiondict['windowfunc'])
        oversampfreq = optiondict['oversampfactor'] / fmritr

        # Step -1 - check the regressor for periodic components in the passband
        dolagmod = True
        doreferencenotch = False
        if optiondict['check_autocorrelation']:
            print('checking reference regressor autocorrelation properties')
            optiondict['lagmod'] = 1000.0
            lagindpad = corrorigin - 2 * np.max((lagmininpts, lagmaxinpts))
            acmininpts = lagmininpts + lagindpad
            acmaxinpts = lagmaxinpts + lagindpad
            thexcorr, dummy = onecorrelation(referencetc, oversampfreq, corrorigin, acmininpts, acmaxinpts, theprefilter,
                                      referencetc,
                                      optiondict)
            outputarray = np.asarray([corrscale[corrorigin - acmininpts:corrorigin + acmaxinpts], thexcorr])
            tide.writenpvecs(outputarray, outputname + '_referenceautocorr_pass' + str(thepass) + '.txt')
            thelagthresh = np.max((abs(optiondict['lagmin']), abs(optiondict['lagmax'])))
            theampthresh = 0.1
            print('searching for sidelobes with amplitude >', theampthresh, 'with abs(lag) <', thelagthresh, 's')
            sidelobetime, sidelobeamp = tide.autocorrcheck(corrscale[corrorigin - acmininpts:corrorigin + acmaxinpts],
                                                           thexcorr, acampthresh=theampthresh,
                                                           aclagthresh=thelagthresh,
                                                           prewindow=optiondict['usewindowfunc'],
                                                           dodetrend=optiondict['dodetrend'])
            if sidelobetime is not None:
                passsuffix = '_pass' + str(thepass + 1)
                optiondict['acsidelobelag' + passsuffix] = sidelobetime
                optiondict['despeckle_thresh'] = np.max([optiondict['despeckle_thresh'], sidelobetime/2.0])
                optiondict['acsidelobeamp' + passsuffix] = sidelobeamp
                print('\n\nWARNING: autocorrcheck found bad sidelobe at', sidelobetime, 'seconds (', 1.0 / sidelobetime,
                      'Hz)...')
                tide.writenpvecs(np.array([sidelobetime]), outputname + '_autocorr_sidelobetime' + passsuffix + '.txt')
                if optiondict['fix_autocorrelation']:
                    print('Removing sidelobe')
                    if dolagmod:
                        print('subjecting lag times to modulus')
                        optiondict['lagmod'] = sidelobetime / 2.0
                    if doreferencenotch:
                        print('removing spectral component at sidelobe frequency')
                        acstopfreq = 1.0 / sidelobetime
                        acfixfilter = tide.noncausalfilter(debug=optiondict['debug'])
                        acfixfilter.settype('arb_stop')
                        acfixfilter.setarb(acstopfreq * 0.9, acstopfreq * 0.95, acstopfreq * 1.05, acstopfreq * 1.1)
                        cleaned_referencetc = tide.stdnormalize(acfixfilter.apply(fmrifreq, referencetc))
                        cleaned_nonosreferencetc = tide.stdnormalize(acfixfilter.apply(fmrifreq, nonosreferencetc))
                        tide.writenpvecs(cleaned_referencetc,
                                         outputname + '_cleanedreference_pass' + str(thepass) + '.txt')
                else:
                    cleaned_referencetc = 1.0 * referencetc
                    cleaned_nonosreferencetc = 1.0 * nonosreferencetc
            else:
                print('no sidelobes found in range')
                cleaned_referencetc = 1.0 * referencetc
                cleaned_nonosreferencetc = 1.0 * nonosreferencetc
        else:
            cleaned_referencetc = 1.0 * referencetc
            cleaned_nonosreferencetc = 1.0 * nonosreferencetc

        # Step 0 - estimate significance
        if optiondict['numestreps'] > 0:
            timings.append(['Significance estimation start, pass ' + str(thepass), time.time(), None, None])
            print('\n\nSignificance estimation, pass ' + str(thepass))
            if optiondict['verbose']:
                print('calling getNullDistributionData with args:', oversampfreq, fmritr, corrorigin, lagmininpts,
                      lagmaxinpts)
            if optiondict['memprofile']:
                getNullDistributionData_func = profile(getNullDistributionData, precision=2)
            else:
                logmem('before getnulldistristributiondata', file=memfile)
                getNullDistributionData_func = getNullDistributionData
            corrdistdata = getNullDistributionData_func(cleaned_referencetc, corrscale, theprefilter,
                                                        oversampfreq, corrorigin, lagmininpts, lagmaxinpts,
                                                        optiondict)
            tide.writenpvecs(corrdistdata, outputname + '_corrdistdata_pass' + str(thepass) + '.txt')

            # calculate percentiles for the crosscorrelation from the distribution data
            optiondict['sighistlen'] = 1000
            thepercentiles = np.array([0.95, 0.99, 0.995, 0.999])
            thepvalnames = []
            for thispercentile in thepercentiles:
                thepvalnames.append("{:.3f}".format(1.0 - thispercentile).replace('.', 'p'))

            pcts, pcts_fit, sigfit = tide.sigFromDistributionData(corrdistdata, optiondict['sighistlen'],
                                                                  thepercentiles, twotail=optiondict['bipolar'],
                                                                  displayplots=optiondict['displayplots'],
                                                                  nozero=optiondict['nohistzero'],
                                                                  dosighistfit=optiondict['dosighistfit'])
            if optiondict['ampthreshfromsig']:
                print('setting ampthresh to the p<', "{:.3f}".format(1.0 - thepercentiles[0]), ' threshhold')
                optiondict['ampthresh'] = pcts[2]
            tide.printthresholds(pcts, thepercentiles, 'Crosscorrelation significance thresholds from data:')
            if optiondict['dosighistfit']:
                tide.printthresholds(pcts_fit, thepercentiles, 'Crosscorrelation significance thresholds from fit:')
                tide.makeandsavehistogram(corrdistdata, optiondict['sighistlen'], 0,
                                          outputname + '_nullcorrelationhist_pass' + str(thepass),
                                          displaytitle='Null correlation histogram, pass' + str(thepass),
                                          displayplots=optiondict['displayplots'], refine=False)
            corrdistdata = None
            timings.append(['Significance estimation end, pass ' + str(thepass), time.time(), optiondict['numestreps'],
                            'repetitions'])

        # Step 1 - Correlation step
        print('\n\nCorrelation calculation, pass ' + str(thepass))
        timings.append(['Correlation calculation start, pass ' + str(thepass), time.time(), None, None])
        if optiondict['memprofile']:
            correlationpass_func = profile(correlationpass, precision=2)
        else:
            logmem('before correlationpass', file=memfile)
            correlationpass_func = correlationpass
        voxelsprocessed_cp, theglobalmaxlist = correlationpass_func(fmri_data_valid[:, optiondict['addedskip']:], fft_fmri_data,
                                                  cleaned_referencetc,
                                                  initial_fmri_x, os_fmri_x,
                                                  fmritr,
                                                  corrorigin, lagmininpts, lagmaxinpts,
                                                  corrmask, corrout, meanval,
                                                  theprefilter,
                                                  optiondict)
        for i in range(len(theglobalmaxlist)):
            theglobalmaxlist[i] = corrscale[theglobalmaxlist[i]]
        tide.makeandsavehistogram(np.asarray(theglobalmaxlist), len(corrscale), 0,
                              outputname + '_globallaghist_pass' + str(thepass),
                              displaytitle='lagtime histogram', displayplots=optiondict['displayplots'],
                              therange=(corrscale[0], corrscale[-1]), refine=False)
        timings.append(['Correlation calculation end, pass ' + str(thepass), time.time(), voxelsprocessed_cp, 'voxels'])

        # Step 2 - correlation fitting and time lag estimation
        print('\n\nTime lag estimation pass ' + str(thepass))
        timings.append(['Time lag estimation start, pass ' + str(thepass), time.time(), None, None])

        if optiondict['memprofile']:
            fitcorr_func = profile(fitcorr, precision=2)
        else:
            logmem('before fitcorr', file=memfile)
            fitcorr_func = fitcorr
        voxelsprocessed_fc = fitcorr_func(genlagtc, initial_fmri_x,
                                          lagtc, slicesize,
                                          corrscale[corrorigin - lagmininpts:corrorigin + lagmaxinpts],
                                          lagmask, lagtimes, lagstrengths, lagsigma, corrout, meanval,
                                          gaussout,
                                          R2, optiondict)
        timings.append(['Time lag estimation end, pass ' + str(thepass), time.time(), voxelsprocessed_fc, 'voxels'])

        # Step 2b - Correlation time despeckle
        if optiondict['despeckle_passes'] > 0:
            print('\n\nCorrelation despeckling pass ' + str(thepass))
            print('\tUsing despeckle_thresh =' + str(optiondict['despeckle_thresh']))
            timings.append(['Correlation despeckle start, pass ' + str(thepass), time.time(), None, None])

            # find lags that are very different from their neighbors, and refit starting at the median lag for the point
            voxelsprocessed_fc_ds = 0
            for despecklepass in range(optiondict['despeckle_passes']):
                print('\n\nCorrelation despeckling subpass ' + str(despecklepass + 1))
                outmaparray[validvoxels] = eval('lagtimes')[:]
                medianlags = ndimage.median_filter(outmaparray.reshape(nativespaceshape), 3).reshape((numspatiallocs))
                initlags = np.where(np.abs(outmaparray - medianlags) > optiondict['despeckle_thresh'], medianlags, -1000000.0)[validvoxels]
                if len(initlags) > 0:
                    voxelsprocessed_fc_ds += fitcorr_func(genlagtc, initial_fmri_x,
                                          lagtc, slicesize,
                                          corrscale[corrorigin - lagmininpts:corrorigin + lagmaxinpts],
                                          lagmask, lagtimes, lagstrengths, lagsigma, corrout, meanval,
                                          gaussout,
                                          R2, optiondict,
                                          initiallags=initlags)
                else:
                    print('Nothing left to do! Terminating despeckling')
                    break


            """
            theheader = nim_hdr
            if fileiscifti:
                theheader['intent_code'] = 3006
            else:
                theheader['dim'][0] = 3
                theheader['dim'][4] = 1
            tide.savetonifti((np.where(np.abs(outmaparray - medianlags) > optiondict['despeckle_thresh'], medianlags, 0.0)).reshape(nativespaceshape), theheader, thesizes,
                             outputname + '_despecklemask_pass' + str(thepass))
            """
            print('\n\n', voxelsprocessed_fc_ds, 'voxels despeckled in', optiondict['despeckle_passes'], 'passes')
            timings.append(['Correlation despeckle end, pass ' + str(thepass), time.time(), voxelsprocessed_fc_ds, 'voxels'])

        # Step 3 - regressor refinement for next pass
        if thepass < optiondict['passes']:
            print('\n\nRegressor refinement, pass' + str(thepass))
            timings.append(['Regressor refinement start, pass ' + str(thepass), time.time(), None, None])
            if optiondict['refineoffset']:
                peaklag, peakheight, peakwidth = tide.gethistprops(lagtimes[np.where(lagmask > 0)],
                                                                   optiondict['histlen'])
                optiondict['offsettime'] = peaklag
                optiondict['offsettime_total'] += peaklag
                print('offset time set to ', optiondict['offsettime'], ', total is ', optiondict['offsettime_total'])

            # regenerate regressor for next pass
            if optiondict['memprofile']:
                refineregressor_func = profile(refineregressor, precision=2)
            else:
                logmem('before refineregressor', file=memfile)
                refineregressor_func = refineregressor
            voxelsprocessed_rr, outputdata, refinemask = refineregressor_func(
                cleaned_nonosreferencetc, fmri_data_valid[:, :], fmritr, shiftedtcs, weights, thepass,
                lagstrengths, lagtimes, lagsigma, R2, theprefilter, optiondict,
                padtrs=numpadtrs, includemask=internalincludemask_valid, excludemask=internalexcludemask_valid)
            normoutputdata = tide.stdnormalize(theprefilter.apply(fmrifreq, outputdata))
            tide.writenpvecs(normoutputdata, outputname + '_refinedregressor_pass' + str(thepass) + '.txt')

            if optiondict['dodetrend']:
                resampnonosref_y = tide.detrend(tide.doresample(initial_fmri_x, normoutputdata, initial_fmri_x,
                                                                method=optiondict['interptype']),
                                                demean=optiondict['dodemean'])
                resampref_y = tide.detrend(tide.doresample(initial_fmri_x, normoutputdata, os_fmri_x,
                                                           method=optiondict['interptype']),
                                           demean=optiondict['dodemean'])
            else:
                resampnonosref_y = tide.doresample(initial_fmri_x, normoutputdata, initial_fmri_x,
                                                   method=optiondict['interptype'])
                resampref_y = tide.doresample(initial_fmri_x, normoutputdata, os_fmri_x,
                                              method=optiondict['interptype'])
            if optiondict['usetmask']:
                resampnonosref_y *= tmask_y
                thefit, R = tide.mlregress(tmask_y, resampnonosref_y)
                resampnonosref_y -= thefit[0, 1] * tmask_y
                resampref_y *= tmaskos_y
                thefit, R = tide.mlregress(tmaskos_y, resampref_y)
                resampref_y -= thefit[0, 1] * tmaskos_y

            # reinitialize lagtc for resampling
            genlagtc = tide.fastresampler(initial_fmri_x, normoutputdata, padvalue=padvalue)
            nonosrefname = '_reference_fmrires_pass' + str(thepass + 1) + '.txt'
            osrefname = '_reference_resampres_pass' + str(thepass + 1) + '.txt'
            tide.writenpvecs(tide.stdnormalize(resampnonosref_y), outputname + nonosrefname)
            tide.writenpvecs(tide.stdnormalize(resampref_y), outputname + osrefname)
            timings.append(
                ['Regressor refinement end, pass ' + str(thepass), time.time(), voxelsprocessed_rr, 'voxels'])

    # Post refinement step 0 - Wiener deconvolution
    if optiondict['dodeconv']:
        timings.append(['Wiener deconvolution start', time.time(), None, None])
        print('\n\nWiener deconvolution')
        reportstep = 1000

        # now allocate the arrays needed for Wiener deconvolution
        wienerdeconv = np.zeros(internalvalidspaceshape, dtype=rt_outfloattype)
        wpeak = np.zeros(internalvalidspaceshape, dtype=rt_outfloattype)

        voxelsprocessed_wiener = wienerpass(numspatiallocs, reportstep, fmri_data_valid, threshval,
                                            optiondict, wienerdeconv, wpeak, resampref_y)
        timings.append(['Wiener deconvolution end', time.time(), voxelsprocessed_wiener, 'voxels'])

    # Post refinement step 1 - GLM fitting to remove moving signal
    if optiondict['doglmfilt'] or optiondict['doprewhiten']:
        timings.append(['GLM filtering start', time.time(), None, None])
        if optiondict['doglmfilt']:
            print('\n\nGLM filtering')
        if optiondict['doprewhiten']:
            print('\n\nPrewhitening')
        reportstep = 1000
        if optiondict['dogaussianfilter'] or (optiondict['glmsourcefile'] is not None):
            if optiondict['glmsourcefile'] is not None:
                print('reading in ', optiondict['glmsourcefile'], 'for GLM filter, please wait')
                if optiondict['textio']:
                    nim_data = tide.readvecs(optiondict['glmsourcefile'])
                else:
                    nim, nim_data, nim_hdr, thedims, thesizes = tide.readfromnifti(optiondict['glmsourcefile'])
            else:
                print('rereading', fmrifilename, ' for GLM filter, please wait')
                if optiondict['textio']:
                    nim_data = tide.readvecs(fmrifilename)
                else:
                    nim, nim_data, nim_hdr, thedims, thesizes = tide.readfromnifti(fmrifilename)
            fmri_data_valid = (nim_data.reshape((numspatiallocs, timepoints))[:, validstart:validend + 1])[validvoxels, :] + 0.0
            #fmri_data_valid = fmri_data[validvoxels, :] + 0.0

            # move fmri_data_valid into shared memory
            if optiondict['sharedmem']:
                print('moving fmri data to shared memory')
                timings.append(['Start moving fmri_data to shared memory', time.time(), None, None])
                if optiondict['memprofile']:
                    numpy2shared_func = profile(numpy2shared, precision=2)
                else:
                    logmem('before movetoshared (glm)', file=memfile)
                    numpy2shared_func = numpy2shared
                fmri_data_valid, fmri_data_valid_shared, fmri_data_valid_shared_shape = numpy2shared_func(fmri_data_valid, rt_floatset)
                timings.append(['End moving fmri_data to shared memory', time.time(), None, None])
            nim_data = None

        # now allocate the arrays needed for GLM filtering
        meanvalue = np.zeros(internalvalidspaceshape, dtype=rt_outfloattype)
        rvalue = np.zeros(internalvalidspaceshape, dtype=rt_outfloattype)
        r2value = np.zeros(internalvalidspaceshape, dtype=rt_outfloattype)
        fitNorm = np.zeros(internalvalidspaceshape, dtype=rt_outfloattype)
        fitcoff = np.zeros(internalvalidspaceshape, dtype=rt_outfloattype)
        if optiondict['sharedmem']:
            datatoremove, dummy, dummy = allocshared(internalvalidfmrishape, rt_outfloatset)
            filtereddata, dummy, dummy = allocshared(internalvalidfmrishape, rt_outfloatset)
        else:
            datatoremove = np.zeros(internalvalidfmrishape, dtype=rt_outfloattype)
            filtereddata = np.zeros(internalvalidfmrishape, dtype=rt_outfloattype)
        if optiondict['doprewhiten']:
            prewhiteneddata = np.zeros(internalvalidfmrishape, dtype=rt_outfloattype)
            arcoffs = np.zeros(internalvalidarmodelshape, dtype=rt_outfloattype)

        if optiondict['memprofile']:
            memcheckpoint('about to start glm noise removal...')
        else:
            logmem('before glm', file=memfile)

        if optiondict['preservefiltering']:
            for i in range(len(validvoxels)):
                fmri_data_valid[i] = theprefilter.apply(optiondict['fmrifreq'], fmri_data_valid[i])
        voxelsprocessed_glm = glmpass(numvalidspatiallocs, reportstep, fmri_data_valid, threshval, lagtc,
                                      optiondict, meanvalue, rvalue, r2value, fitcoff, fitNorm,
                                      datatoremove, filtereddata)
        fmri_data_valid = None

        timings.append(['GLM filtering end, pass ' + str(thepass), time.time(), voxelsprocessed_glm, 'voxels'])
        if optiondict['memprofile']:
            memcheckpoint('...done')
        else:
            logmem('after glm filter', file=memfile)
        if optiondict['doprewhiten']:
            arcoff_ref = pacf_yw(resampref_y, nlags=optiondict['armodelorder'])[1:]
            print('\nAR coefficient(s) for reference waveform: ', arcoff_ref)
            resampref_y_pw = rt_floatset(prewhiten(resampref_y, arcoff_ref))
        else:
            resampref_y_pw = rt_floatset(resampref_y)
        if optiondict['usewindowfunc']:
            referencetc_pw = tide.stdnormalize(
                tide.windowfunction(np.shape(resampref_y_pw)[0], type=optiondict['windowfunc']) * tide.detrend(tide.stdnormalize(resampref_y_pw))) / \
                             np.shape(resampref_y_pw)[0]
        else:
            referencetc_pw = tide.stdnormalize(tide.detrend(tide.stdnormalize(resampref_y_pw))) / np.shape(
                resampref_y_pw)[0]
        print('')
        if optiondict['displayplots']:
            fig = figure()
            ax = fig.add_subplot(111)
            ax.set_title('initial and prewhitened reference')
            plot(os_fmri_x, referencetc, os_fmri_x, referencetc_pw)
    else:
        # get the original data to calculate the mean
        print('rereading', fmrifilename, ' for GLM filter, please wait')
        if optiondict['textio']:
            nim_data = tide.readvecs(fmrifilename)
        else:
            nim, nim_data, nim_hdr, thedims, thesizes = tide.readfromnifti(fmrifilename)
        fmri_data = nim_data.reshape((numspatiallocs, timepoints))[:, validstart:validend + 1]
        meanvalue = np.mean(fmri_data, axis=1)

    # Post refinement step 2 - prewhitening
    if optiondict['doprewhiten']:
        print('Step 3 - reprocessing prewhitened data')
        timings.append(['Step 3 start', time.time(), None, None])
        dummy, dummy = correlationpass(prewhiteneddata, fft_fmri_data, referencetc_pw,
                        initial_fmri_x, os_fmri_x,
                        fmritr,
                        corrorigin, lagmininpts, lagmaxinpts,
                        corrmask, corrout, meanval,
                        theprefilter,
                        optiondict)

    # Post refinement step 3 - make and save interesting histograms
    timings.append(['Start saving histograms', time.time(), None, None])
    tide.makeandsavehistogram(lagtimes[np.where(lagmask > 0)], optiondict['histlen'], 0, outputname + '_laghist',
                              displaytitle='lagtime histogram', displayplots=optiondict['displayplots'], refine=False)
    tide.makeandsavehistogram(lagstrengths[np.where(lagmask > 0)], optiondict['histlen'], 0,
                              outputname + '_strengthhist',
                              displaytitle='lagstrength histogram', displayplots=optiondict['displayplots'],
                              therange=(0.0, 1.0))
    tide.makeandsavehistogram(lagsigma[np.where(lagmask > 0)], optiondict['histlen'], 1, outputname + '_widthhist',
                              displaytitle='lagsigma histogram', displayplots=optiondict['displayplots'])
    if optiondict['doglmfilt']:
        tide.makeandsavehistogram(r2value[np.where(lagmask > 0)], optiondict['histlen'], 1, outputname + '_Rhist',
                                  displaytitle='correlation R2 histogram', displayplots=optiondict['displayplots'])
    timings.append(['Finished saving histograms', time.time(), None, None])

    # Post refinement step 4 - save out all of the important arrays to nifti files
    # write out the options used
    tide.writedict(optiondict, outputname + '_options.txt')

    if fileiscifti:
        outsuffix3d = '.dscalar'
        outsuffix4d = '.dtseries'
    else:
        outsuffix3d = ''
        outsuffix4d = ''

    # do ones with one time point first
    timings.append(['Start saving maps', time.time(), None, None])
    if not optiondict['textio']:
        theheader = nim_hdr
        if fileiscifti:
            theheader['intent_code'] = 3006
        else:
            theheader['dim'][0] = 3
            theheader['dim'][4] = 1

    # first generate the MTT map
    MTT = np.square(lagsigma) - (optiondict['acwidth'] * optiondict['acwidth'])
    MTT = np.where(MTT > 0.0, np.sqrt(MTT), 0.0)

    for mapname in ['lagtimes', 'lagstrengths', 'R2', 'lagsigma', 'lagmask', 'MTT']:
        if optiondict['memprofile']:
            memcheckpoint('about to write ' + mapname)
        else:
            logmem('about to write ' + mapname, file=memfile)
        outmaparray[validvoxels] = eval(mapname)[:]
        if optiondict['textio']:
            tide.writenpvecs(outmaparray.reshape(nativespaceshape, 1),
                             outputname + '_' + mapname + outsuffix3d + '.txt')
        else:
            tide.savetonifti(outmaparray.reshape(nativespaceshape), theheader, thesizes,
                             outputname + '_' + mapname + outsuffix3d)

    if optiondict['doglmfilt']:
        for mapname, mapsuffix in [('rvalue', 'fitR'), ('r2value', 'fitR2'), ('meanvalue', 'mean'),
                                   ('fitcoff', 'fitcoff'), ('fitNorm', 'fitNorm')]:
            if optiondict['memprofile']:
                memcheckpoint('about to write ' + mapname)
            else:
                logmem('about to write ' + mapname, file=memfile)
            outmaparray[validvoxels] = eval(mapname)[:]
            if optiondict['textio']:
                tide.writenpvecs(outmaparray.reshape(nativespaceshape),
                                 outputname + '_' + mapsuffix + outsuffix3d + '.txt')
            else:
                tide.savetonifti(outmaparray.reshape(nativespaceshape), theheader, thesizes,
                                 outputname + '_' + mapsuffix + outsuffix3d)
        rvalue = None
        r2value = None
        meanvalue = None
        fitcoff = None
        fitNorm = None
    else:
        for mapname, mapsuffix in [('meanvalue', 'mean')]:
            if optiondict['memprofile']:
                memcheckpoint('about to write ' + mapname)
            else:
                logmem('about to write ' + mapname, file=memfile)
            outmaparray = eval(mapname)[:]
            if optiondict['textio']:
                tide.writenpvecs(outmaparray.reshape(nativespaceshape),
                                 outputname + '_' + mapsuffix + outsuffix3d + '.txt')
            else:
                tide.savetonifti(outmaparray.reshape(nativespaceshape), theheader, thesizes,
                                 outputname + '_' + mapsuffix + outsuffix3d)
        meanvalue = None

    if optiondict['numestreps'] > 0:
        for i in range(0, len(thepercentiles)):
            pmask = np.where(np.abs(lagstrengths) > pcts[i], lagmask, 0 * lagmask)
            if optiondict['dosighistfit']:
                tide.writenpvecs(sigfit, outputname + '_sigfit' + '.txt')
            tide.writenpvecs(np.array([pcts[i]]), outputname + '_p_lt_' + thepvalnames[i] + '_thresh.txt')
            outmaparray[validvoxels] = pmask[:]
            if optiondict['textio']:
                tide.writenpvecs(outmaparray.reshape(nativespaceshape),
                                 outputname + '_p_lt_' + thepvalnames[i] + '_mask' + outsuffix3d + '.txt')
            else:
                tide.savetonifti(outmaparray.reshape(nativespaceshape), theheader, thesizes,
                                 outputname + '_p_lt_' + thepvalnames[i] + '_mask' + outsuffix3d)

    if optiondict['passes'] > 1:
        outmaparray[validvoxels] = refinemask[:]
        if optiondict['textio']:
            tide.writenpvecs(outfmriarray.reshape(nativefmrishape),
                             outputname + '_lagregressor' + outsuffix4d + '.txt')
        else:
            tide.savetonifti(outmaparray.reshape(nativespaceshape), theheader, thesizes,
                             outputname + '_refinemask' + outsuffix3d)
        refinemask = None

    # clean up arrays that will no longer be needed
    lagtimes = None
    lagstrengths = None
    lagsigma = None
    R2 = None
    lagmask = None

    # now do the ones with other numbers of time points
    if not optiondict['textio']:
        theheader = nim_hdr
        if fileiscifti:
            theheader['intent_code'] = 3002
        else:
            theheader['dim'][4] = np.shape(corrscale)[0]
        theheader['toffset'] = corrscale[corrorigin - lagmininpts]
        theheader['pixdim'][4] = corrtr
    outcorrarray[validvoxels, :] = gaussout[:, :]
    if optiondict['textio']:
        tide.writenpvecs(outcorrarray.reshape(nativecorrshape),
                         outputname + '_gaussout' + outsuffix4d + '.txt')
    else:
        tide.savetonifti(outcorrarray.reshape(nativecorrshape), theheader, thesizes,
                         outputname + '_gaussout' + outsuffix4d)
    gaussout = None
    outcorrarray[validvoxels, :] = corrout[:, :]
    if optiondict['textio']:
        tide.writenpvecs(outcorrarray.reshape(nativecorrshape),
                         outputname + '_corrout' + outsuffix4d + '.txt')
    else:
        tide.savetonifti(outcorrarray.reshape(nativecorrshape), theheader, thesizes,
                         outputname + '_corrout' + outsuffix4d)
    corrout = None
    if optiondict['saveprewhiten']:
        if not optiondict['textio']:
            theheader = nim.header
            theheader['toffset'] = 0.0
            if fileiscifti:
                theheader['intent_code'] = 3002
            else:
                theheader['dim'][4] = optiondict['armodelorder']
        outarmodelarray[validvoxels, :] = arcoffs[:, :]
        if optiondict['textio']:
            tide.writenpvecs(outarmodelarray.reshape(nativearmodelshape),
                             outputname + '_arN' + outsuffix4d + '.txt')
        else:
            tide.savetonifti(outarmodelarray.reshape(nativearmodelshape), theheader, thesizes,
                             outputname + '_arN' + outsuffix4d)
        arcoffs = None

    if not optiondict['textio']:
        theheader = nim_hdr
        theheader['pixdim'][4] = fmritr
        theheader['toffset'] = 0.0
        if fileiscifti:
            theheader['intent_code'] = 3002
        else:
            theheader['dim'][4] = np.shape(initial_fmri_x)[0]

    if optiondict['savelagregressors']:
        outfmriarray[validvoxels, :] = lagtc[:, :]
        if optiondict['textio']:
            tide.writenpvecs(outfmriarray.reshape(nativefmrishape),
                             outputname + '_lagregressor' + outsuffix4d + '.txt')
        else:
            tide.savetonifti(outfmriarray.reshape(nativefmrishape), theheader, thesizes,
                             outputname + '_lagregressor' + outsuffix4d)
        lagtc = None

    if optiondict['passes'] > 1:
        if optiondict['savelagregressors']:
            outfmriarray[validvoxels, :] = shiftedtcs[:, :]
            if optiondict['textio']:
                tide.writenpvecs(outfmriarray.reshape(nativefmrishape),
                             outputname + '_shiftedtcs' + outsuffix4d + '.txt')
            else:
                tide.savetonifti(outfmriarray.reshape(nativefmrishape), theheader, thesizes,
                             outputname + '_shiftedtcs' + outsuffix4d)
        shiftedtcs = None

    if optiondict['doglmfilt'] and optiondict['saveglmfiltered']:
        datatoremove = None
        outfmriarray[validvoxels, :] = filtereddata[:, :]
        if optiondict['textio']:
            tide.writenpvecs(outfmriarray.reshape(nativefmrishape),
                             outputname + '_filtereddata' + outsuffix4d + '.txt')
        else:
            tide.savetonifti(outfmriarray.reshape(nativefmrishape), theheader, thesizes,
                             outputname + '_filtereddata' + outsuffix4d)
        filtereddata = None

    if optiondict['saveprewhiten']:
        outfmriarray[validvoxels, :] = prewhiteneddata[:, :]
        if optiondict['textio']:
            tide.writenpvecs(outfmriarray.reshape(nativefmrishape),
                             outputname + '_prewhiteneddata' + outsuffix4d + '.txt')
        else:
            tide.savetonifti(outfmriarray.reshape(nativefmrishape), theheader, thesizes,
                             outputname + '_prewhiteneddata' + outsuffix4d)
        prewhiteneddata = None

    timings.append(['Finished saving maps', time.time(), None, None])
    memfile.close()
    print('done')

    if optiondict['displayplots']:
        show()
    timings.append(['Done', time.time(), None, None])

    # Post refinement step 5 - process and save timing information
    nodeline = 'Processed on ' + platform.node()
    tide.proctiminginfo(timings, outputfile=outputname + '_runtimings.txt', extraheader=nodeline)


if __name__ == '__main__':
    main()
