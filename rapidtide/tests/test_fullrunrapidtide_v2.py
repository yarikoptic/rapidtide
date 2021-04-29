#!/usr/bin/env python
# -*- coding: latin-1 -*-
#
#   Copyright 2016-2021 Blaise Frederick
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
import argparse
import os

import matplotlib as mpl

import rapidtide.workflows.rapidtide as rapidtide_workflow
import rapidtide.workflows.rapidtide_parser as rapidtide_parser
from rapidtide.tests.utils import create_dir, get_examples_path, get_test_temp_path, mse


def test_fullrunrapidtide(debug=False, display=False):
    # run rapidtide
    inputargs = [
        os.path.join(get_examples_path(), "sub-RAPIDTIDETEST.nii.gz"),
        os.path.join(get_test_temp_path(), "sub-RAPIDTIDETEST"),
        "--tmask",
        os.path.join(get_examples_path(), "tmask3.txt"),
        "--corrmask",
        os.path.join(get_examples_path(), "sub-RAPIDTIDETEST_mask.nii.gz"),
        "--globalmeaninclude",
        os.path.join(get_examples_path(), "sub-RAPIDTIDETEST_mask.nii.gz"),
        "--globalmeanexclude",
        os.path.join(get_examples_path(), "sub-RAPIDTIDETEST_nullmask.nii.gz"),
        "--refineinclude",
        os.path.join(get_examples_path(), "sub-RAPIDTIDETEST_mask.nii.gz"),
        "--refineexclude",
        os.path.join(get_examples_path(), "sub-RAPIDTIDETEST_nullmask.nii.gz"),
        "--savelags",
        "--checkpoint",
        "--saveintermediatemaps",
        "--nolimitoutput",
        "--calccoherence",
        "--nprocs",
        "1",
        "--passes",
        "2",
        "--numnull",
        "0",
        "--globalsignalmethod",
        "meanscale",
    ]
    rapidtide_workflow.rapidtide_main(rapidtide_parser.process_args(inputargs=inputargs))


def main():
    test_fullrunrapidtide(debug=True, display=True)


if __name__ == "__main__":
    mpl.use("TkAgg")
    main()
