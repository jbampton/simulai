# (C) Copyright IBM Corp. 2019, 2020, 2021, 2022.

#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at

#           http://www.apache.org/licenses/LICENSE-2.0

#     Unless required by applicable law or agreed to in writing, software
#     distributed under the License is distributed on an "AS IS" BASIS,
#     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#     See the License for the specific language governing permissions and
#     limitations under the License.

import numpy as np
import pickle
import os
import matplotlib.pyplot as plt
from argparse import ArgumentParser

from simulai.rom import POD
from simulai.math.differentiation import CollocationDerivative
from simulai.rom import QQM
from simulai.metrics import L2Norm

# MAIN
# Reading command line arguments.
parser = ArgumentParser(description="Reading input parameters")
parser.add_argument('--data_path', help="Path to the u npy file.", type=str)
parser.add_argument('--norm', help="Using normalization. (False or True)", type=bool, default=True)
parser.add_argument('--train_fraction', help="Train fraction", type=float, default=0.90)
parser.add_argument('--n_components', help="Number of components", type=int, default=100)
parser.add_argument('--mean_component', help="Use mean component or not", type=bool, default=True)
parser.add_argument('--dt', help="Time discretization step.", type=float, default=5e-2)

args = parser.parse_args()

data_path = args.data_path
norm = args.norm
train_fraction = args.train_fraction
n_components = args.n_components
mean_component = args.mean_component
dt = args.dt

recalculate_derivative = False
norm_time_series_singular_values = True  # False
norm_time_series_minmax = None  # True
validation_fraction = (1 - train_fraction) / 2
test_fraction = validation_fraction
use_plot = True

T_discard = 200
discard = int(T_discard / dt)

# Getting up the upper directory of data_path
save_path = os.path.dirname(data_path)

data = np.load(data_path)[discard::10]

n_samples = data.shape[0]
n_samples_train = int(train_fraction * n_samples)
n_samples_validation = int(validation_fraction * n_samples)
n_samples_test = int(test_fraction * n_samples)

data_train = data[:n_samples_train]
data_validation = data[n_samples_train:n_samples_train + n_samples_validation]
data_test = data[n_samples_train + n_samples_validation:]

max_train = data_train.max(0)
min_train = data_train.min(0)

times = np.arange(0, data.shape[0], 1) * dt
x = np.arange(0, data.shape[1])

# Use normalization or not
if norm is not True:

    data_norm = data
    data_train_norm = data_train
    data_validation_norm = data_validation
    data_test_norm = data_test

else:

    data_norm = 2 * (data - min_train) / (max_train - min_train) - 1
    data_train_norm = 2 * (data_train - min_train) / (max_train - min_train) - 1
    data_validation_norm = 2 * (data_validation - min_train) / (max_train - min_train) - 1
    data_test_norm = 2 * (data_test - min_train) / (max_train - min_train) - 1

pca = POD(config={"n_components": n_components, "mean_component": mean_component})

pca.fit(data=data_train_norm)

projected = pca.project(data=data_test_norm)

reconstructed = pca.reconstruct(projected_data=projected)

diff = reconstructed - data_test_norm

error = 100 * np.linalg.norm(diff.flatten(), 2) / np.linalg.norm(data_test_norm.flatten(), 2)

print(f"Projection error: {error} %\n")

diff = CollocationDerivative(config={'step': dt})

times = np.arange(0, data.shape[0]) * dt

projected = pca.project(data=data_norm)

reconstructed = pca.reconstruct(projected_data=projected)

error = data_norm - reconstructed

qqm = QQM(n_inputs=projected.shape[-1], lambd=1e-5, alpha_0=1e-2, epsilon=1e-3, use_mean=True)
qqm.fit(input_data=projected, target_data=error)


projected = pca.project(data=data_test_norm)
reconstructed = pca.reconstruct(projected_data=projected) + qqm.eval(data=projected)

l2_norm = L2Norm()

error = 100*l2_norm(data=reconstructed, reference_data=data_test_norm, relative_norm=True)

print(f"Projection error: {error} %")








