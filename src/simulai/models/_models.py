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
import json
import importlib
import inspect
from collections import OrderedDict
import pickle
import warnings

from simulai.models import ModelMaker
from simulai.parallel import PipelineMPI

MPI_GLOBAL_AVAILABILITY = True

try:
    from mpi4py import MPI
except:
    MPI_GLOBAL_AVAILABILITY = False
    warnings.warn(f'Trying to import MPI in {__file__}.')
    warnings.warn('mpi4py is not installed. If you want to execute MPI jobs, we recommend you install it.')


def exec_model_wrapper(input_data=None, target_data=None, model=None, key=None):

    model.fit(input_data=input_data, target_data=target_data)

    return {key: model}

# It executes a parallel fitting process using multiple sub-models following the approach
# shown in Vlachas et al, https://arxiv.org/abs/1910.05266
class ModelPool:

    def __init__(self, config: dict=None,
                       model_type: str='EchoStateNetwork',
                       model_config: dict=None,
                       parallel: str=None) -> None:

        self.stencil_size = -1
        self.skip_size = None
        self.group_size = None
        self.optimizers_list = None
        self.template = None
        self.n_inputs = None
        self.n_outputs = None
        self.n_auxiliary = None

        self.model_type = model_type
        self.model_instances_list = OrderedDict()

        assert parallel in [None, 'mpi'], f"The option {parallel} is not supported for parallel."
        self.compute_parallel = parallel

        if self.compute_parallel == 'mpi':
            self._sub_model_pool_dispatcher = self._sub_model_parallel_mpi_pool_dispatcher
        elif self.compute_parallel is None:
            self._sub_model_pool_dispatcher = self._sub_model_serial_pool_dispatcher

        self.regressions_module = 'simulai.regression'

        # The ModelPool instance needs to receive information about
        # these variables seen below
        self.essential_tags = ['group_size', 'stencil_size', 'skip_size']
        self.optional_keys = ['n_inputs', 'n_outputs', 'n_auxiliary']

        self.fundamental_tags =  self.essential_tags + ['optimizers_list']

        # ModelPool configuration
        # Check if the ModelPool configuration is complete
        if not all([item in config.keys() for item in self.essential_tags]):
            assert 'template' in config.keys(), "If the configuration is incomplete, a template must be provided"
            self.config = self._get_template(name=config['template'])
            self.template = config['template']
        else:
            self.config = config

        # Basic restrictions
        for tag in self.config:
            if tag in self.fundamental_tags:
                assert tag in self.config, "It is necessary to provide a value to {}".format(tag)
            else:
                pass
            setattr(self, tag, self.config[tag])

        # Preserving the pool configuration in order to
        # restore it
        self.config_pool = config

        for key in self.optional_keys:
            setattr(self, key, config.get(key, None))

        ### Commentary about the templates options

        # The option "independent_series" takes all the field variables
        # and all the auxiliary variables to individually predict each time-series

        # The option "bo_communication_series" takes a field time-series and all the
        # auxiliary ones to predict the field time-series itself

        if self.template == 'independent_series':
            no_parallelism_signal = 1
        elif self.template == 'no_communication_series':
            no_parallelism_signal = 2
        else:
            no_parallelism_signal = 0

        self.model_config = model_config

        self.data_residuals = list()
        self.residuals_type = list()
        self.losses = list()

        self.default_residual = 'surrogate'
        self.default_loss = 'square-mean'
        self.independent_case = ('independent_series', 'no_communication_series')

        self.wrapper_model = None

        # It is used when the sub-networks are depending on history
        self.history_sizes = OrderedDict()

        self.input_data_preparer = self._by_pass_input
        self.train_input_data_preparer = self._by_pass_input

        self.is_it_a_raw_model = True

        self.no_group_value = -1

        self.n_groups = None

        if self.group_size == self.no_group_value:
            self.group_size = self.n_inputs

        assert 0 == (self.stencil_size % 2), "stencil_size must be divisible by 2"

        self.stencil = int(self.stencil_size / 2)

        self._configure_parameters(no_parallelism_signal)

        self.no_parallelism_signal = no_parallelism_signal

    @property
    def sub_models(self):
        return list(self.model_instances_list.values())

    @property
    def sub_models_keys(self):
        return list(self.model_instances_list.keys())

    # Pre-defined templates
    def _get_template(self, name=None):

        templates = {'independent_series': {
                                            'group_size': 1,
                                            'stencil_size': 0,
                                            'skip_size': 1
                                           },
                     'no_communication_series': {
                                            'group_size': 1,
                                            'stencil_size': 0,
                                            'skip_size': 1
                                          },
                     'no_parallelism': {
                                            'group_size': -1,
                                            'stencil_size': 0,
                                            'skip_size': 1
                                        }
                    }

        template = templates.get(name, None)

        if template:
            return template
        else:
            raise Exception(f"The template {name} is not available.")

    def _construct_subdatasets(self, groups_indices, data, auxiliary_data=None):

        sub_datasets = OrderedDict()

        for ig, group in enumerate(groups_indices):

            dataset_id = self._make_id(ig)

            if auxiliary_data is None:
                sub_group = data[..., slice(*group)]
            else:
                sub_group = np.hstack([data[..., slice(*group)], auxiliary_data])

            sub_datasets[dataset_id] = sub_group

        return sub_datasets

    def _adequate_input_history(self, sub_datasets):

        new_sub_datasets = OrderedDict()

        for group_id, dataset in sub_datasets.items():
            history_size = self.history_sizes[group_id]
            new_sub_datasets[group_id] = dataset[:, -history_size:, ...]

        return new_sub_datasets

    # It constructs the indices intervals of the input datasets for each sub-group
    def _construct_input_groups(self, no_subdivision=None):

        groups_indices_input = list()

        # When the option no_subdivision is active all the input series
        # are used for all the sub-groups
        if not no_subdivision:
            groups_indices_input.append((0, self.input_size + self.stencil_size))

            for group in self.groups_indices_target[1:-1]:

                first, second = group

                groups_indices_input.append((first - self.stencil,
                                             second + self.stencil))

            if self.n_groups > 1:
                groups_indices_input.append((self.n_inputs - self.input_size - self.stencil_size - self.n_auxiliary,
                                             self.n_inputs - self.n_auxiliary))
        # It uses all the time-series repeated for each sub-group
        elif no_subdivision == 1:
            for i in range(len(self.groups_indices_target)):
                groups_indices_input.append((0, self.n_inputs)) # Considering all the time-series
        elif no_subdivision == 2:
            for i in range(len(self.groups_indices_target)):
                groups_indices_input.append((i, i+1))  # Considering just the current time-series
        else:
            raise Exception(f"The option {no_subdivision} is not supported.")

        return groups_indices_input

    # It constructs the indices intervals of the target datasets for each sub-group
    def _construct_target_groups(self):

        indices = np.arange(0, self.n_outputs + self.group_size, self.group_size)
        indices_first = indices[:-1]
        indices_second = indices[1:]

        groups_indices_target = [(first, second) for first, second
                                 in zip(indices_first, indices_second)]

        return groups_indices_target

    def _construct_config_dict_to(self, model_config, method):

        kwargs = inspect.getfullargspec(method).args

        return {key: value for key, value in model_config.items()
                     if key in kwargs}

    def _history_dependent_input(self, model_id=None, previous_data=None, data=None):

        history_size_ = self.history_sizes.get(model_id)

        history_size = (history_size_ or self.max_history)

        return np.concatenate([previous_data, data], axis=1)[:, -history_size:, ...]

    def _train_history_dependent_input(self, model_id=None, data=None):

        history_data = self.history_sizes.get(model_id)

        return data[:history_data, ...]

    def _by_pass_input(self, model_id=None, previous_data=None, data=None):

        return data

    @property
    def max_history(self):

        history_sizes = list(self.history_sizes.values())

        return max(history_sizes)

    def _make_id(self, idx=None):

        return self.model_type + '_' + str(idx)

    def _dataset(self, data=None, shuffle=False):

        assert type(data) is list, f"The input must be a list of datasets, but received type {type(data)}"

        if shuffle:

            dim = data[0].shape[0]
            samples_indices = np.arange(dim)
            np.random.shuffle(samples_indices)

            return tuple([item[samples_indices] for item in data])

        else:
            return tuple(data)

    def _configure_parameters(self, no_parallelism_signal):

        group_dimension = self.group_size

        self.input_size = group_dimension

        # Keeping the variable input_size inside model_config
        # for future use
        if no_parallelism_signal == 1:
            self.input_size_ = self.n_inputs
        elif no_parallelism_signal == 2:
            self.input_size_ = self.n_auxiliary + 1
        else:
            self.input_size_ = self.input_size + self.n_auxiliary

        # Constructing the groups indices for the target variables
        self.groups_indices_target = self._construct_target_groups()
        self.n_groups = len(self.groups_indices_target)

        # Constructing the groups indices for the input variables
        self.groups_indices_input = self._construct_input_groups(no_subdivision=no_parallelism_signal)

    # It prepares the datasets and the more important parameters
    def _configure_data(self, input_data=None, target_data=None, auxiliary_data=None):

        # It is expected input_data and target_data have the
        # shape (n_times, n_series)

        # Executing the pool configuration
        self.n_auxiliary = auxiliary_data.shape[1] if auxiliary_data is not None else 0
        n_inputs_assert = input_data.shape[-1] + self.n_auxiliary
        assert n_inputs_assert == self.n_inputs, f"The dataset provided must have {self.n_inputs}" \
                                                 f"columns, but received {n_inputs_assert}."

        if isinstance(target_data, np.ndarray):
            assert target_data.shape[-1] == self.n_outputs, f"The dataset provided must have {self.n_outputs}" \
                                                            f"columns, but received {target_data.shape[-1]}."
            self.n_outputs = target_data.shape[-1]
        else:
            self.n_outputs = input_data.shape[-1]

    def _get_sub_datasets(self, input_data=None, target_data=None, auxiliary_data=None):

        sub_datasets = self._construct_subdatasets(self.groups_indices_input, input_data,
                                                   auxiliary_data=auxiliary_data)

        sub_datasets_target = self._construct_subdatasets(self.groups_indices_target, target_data)

        return sub_datasets, sub_datasets_target

    @property
    def model_ids_list(self):
        return list(self.model_instances_list.keys())

    def _configure_list_of_models(self, n_groups=None, regression=None, model_config=None):

        for net_id in range(n_groups):

            self._configure_single_model(model_config, net_id=net_id, regression=regression)

    def _configure_single_model(self, model_config, net_id=None, regression=None) -> None:

        model_id = self._make_id(idx=net_id)
        model = self.model_instances_list.get(model_id, None)

        if model is None:

            print("This ModelPool is raw. Executing configuration.")

            model_config['model_id'] = model_id

            var_input_names = ['var_' + str(i) for i in range(self.input_size)]
            var_target_names = ['var_' + str(i) + '_o' for i in range(self.group_size)]

            model_config['inputs_names'] = [var_input_names]
            model_config['outputs_names'] = [var_target_names]
            model_config['number_of_inputs'] = self.input_size_ + self.stencil_size # self.n_auxiliary is already
                                                                                    # included in self.input_size_

            model_config = self._construct_config_dict_to(model_config, regression)

            model_instance = regression(**model_config)

            self.model_instances_list[model_id] = model_instance

            # It is just applied to models who depends on history.
            if model_instance.depends_on_history:

                assert model_instance.horizon_size == 1, "A sub-model cannot extrapolate" \
                                                         " more than one time-step per iteration."

                self.history_sizes[model_id] = model_instance.history_size

            else:
                pass
        else:
            pass

    def _get_regression_class(self):
        # The module in which are defined the regression classes.
        engine_module = importlib.import_module(self.regressions_module)
        # Getting up the correspondent regression class to be instantiated.
        # At the moment, the regression must be the same for all the sub-models
        regression = getattr(engine_module, self.model_type) # TODO It could be specific for each
                                                             #  sub-model

        return regression

    # Configuring and instantiating the sub-models,
    def _configure_model(self, model_config=None, index=None):

        # Instantiating each sub-network. They are expected to
        # employ the same regression technique.
        regression = self._get_regression_class()

        # _configure_single_model will basically instantiate the list of model instances
        if index is not None:

            self._configure_single_model(model_config, net_id=index, regression=regression)

        else:

            if self.compute_parallel is None:

                self._configure_list_of_models(n_groups=self.n_groups, regression=regression,
                                               model_config=model_config)

            # When MPI is used, it is necessary to avoid repetition during the processing.
            elif self.compute_parallel == 'mpi':

                comm = MPI.COMM_WORLD
                rank = comm.Get_rank()

                if rank == 0:
                    self._configure_list_of_models(n_groups=self.n_groups, regression=regression,
                                                   model_config=model_config)

                print(f"Broadcasting value from rank 0.")
                self.model_instances_list = comm.bcast(self.model_instances_list, root=0)

                comm.barrier()

    # Executing each sub-model a time
    def _single_sub_model_dispatcher(self, model_id, sub_datasets_model_id, sub_datasets_target_model_id, shuffle=False):

        # An auto-executable model can be trained without any wrapper class (as ModelMaker).
        # Basically is a method which does not use optimization
        # (as certain classes of Reservoir Computing).

        model = self.model_instances_list.get(model_id)

        is_autoexecutable = hasattr(model, 'fit')

        if is_autoexecutable:

            print("Executing the model {}".format(model_id))

            # Shuffling could be a strange stuff here, but here is it.
            dataset_, dataset_target_ = self._dataset(data=[sub_datasets_model_id, sub_datasets_target_model_id],
                                                      shuffle=shuffle)

            model.fit(input_data=dataset_, target_data=dataset_target_)

        else:
            raise Exception(f"{model} is not auto-executable.")

    # Loop for dispatching for a list of sub-models
    def _sub_model_serial_pool_dispatcher(self, sub_datasets=None, sub_datasets_target=None,
                                                model_instances_list=None, shuffle=False):

        # Serial dispatcher
        for model_id, model in model_instances_list.items():

            print("Executing the model {}".format(model_id))

            # Getting the correspond datasets for each model
            dataset = sub_datasets[model_id]
            dataset_target = sub_datasets_target[model_id]

            # Executing shuffling, if necessary
            dataset_, dataset_target_ = self._dataset(data=[dataset, dataset_target], shuffle=shuffle)

            # Fitting the model instance
            model.fit(input_data=dataset_, target_data=dataset_target_)

    # Loop for dispatching list of sub-models in parallel using the MPI API
    def _sub_model_parallel_mpi_pool_dispatcher(self, sub_datasets: dict=None, sub_datasets_target: dict=None,
                                                  model_instances_list: dict=None, shuffle: bool=False) -> None:

        # Lists to be used for dispatching the sub-processes in parallel
        datasets = list()
        datasets_target = list()
        models = list()
        keys = list()

        comm = MPI.COMM_WORLD
        rank = comm.Get_rank()

        if rank == 0:
            # Preparing sub-datasets and sub-models
            for model_id, model in model_instances_list.items():

                print("Preparing the model {}".format(model_id))

                # Getting the correspond datasets for each model
                dataset = sub_datasets[model_id]
                dataset_target = sub_datasets_target[model_id]

                # Executing shuffling, if necessary
                dataset_, dataset_target_ = self._dataset(data=[dataset, dataset_target], shuffle=shuffle)

                datasets.append(dataset_)
                datasets_target.append(dataset_target_)

                models.append(model)
                keys.append(model_id)

        datasets = comm.bcast(datasets, root=0)
        datasets_target = comm.bcast(datasets_target, root=0)
        models = comm.bcast(models, root=0)
        keys = comm.bcast(keys, root=0)

        comm.barrier()

        kwargs = {'input_data': datasets,
                  'target_data': datasets_target,
                  'model': models,
                  'key': keys}

        # Pipeline for executing MPI jobs for independent sub-process
        mpi_run = PipelineMPI(exec=exec_model_wrapper, collect=True)

        # Fitting the model instances in parallel
        mpi_run.run(kwargs=kwargs)

        if mpi_run.success:
            self.model_instances_list = mpi_run.status_dict

        return mpi_run.success

    # Executing the fitting processes for all the sub-models
    def _sub_model_dispatcher(self, sub_datasets, sub_datasets_target, shuffle=False):

        # An auto-executable model can be trained without any wrapper class (as ModelMaker).
        # Basically is a method with does not use back-propagation optimization
        # (as Reservoir Computing and Extreme Learning Machines classes).
        msg = None

        if self._all_autoexecutable:

            # Dispatching processes serially or in parallel
            msg = self._sub_model_pool_dispatcher(sub_datasets=sub_datasets, sub_datasets_target=sub_datasets_target,
                                                  model_instances_list=self.model_instances_list, shuffle=shuffle)

        # In case of handy models, we use a ModelMaker class to handle
        # the sub-networks
        elif not self._all_autoexecutable:

            assert 'optimizers_list' in self.config, 'In case of not auto-executable' \
                                                     'models, it is necessary to provide ' \
                                                     'an optimizers list'

            optimizers_list = self.config.get('optimizers_list')
            residuals_type = self.n_groups*self.group_size*[self.default_residual]
            losses = self.n_groups*self.group_size*[self.default_loss]

            models_instance_list = list(self.model_instances_list.values())
            input_data = list(sub_datasets.values())
            target_data = list(sub_datasets_target.values())

            self.wrapper_model = ModelMaker(regressions=models_instance_list,
                                            optimizers_list=optimizers_list,
                                            residuals_type=residuals_type,
                                            losses=losses,
                                            data_residuals=list(sub_datasets.keys()))

            # ModelMaker has its own shuffle mechanism
            self.wrapper_model.fit(input_data_list=input_data,
                                   target_data_list=target_data,
                                   shuffle=shuffle)

        else:

            raise Exception('At the moment, all the models must be auto-executable or not.'
                            'it is not possible to mix them, let us say, in a hybrid model.')

        return msg

    # If auxiliary variables (such as forcings) are required, at each iteration
    # the input is equivalent to output of the previous one concatenated to the
    # correspondent auxiliary variables array.
    def _stack_auxiliary(self, data=None, auxiliary_data=None, step=None):

        return np.hstack([data, auxiliary_data[step][None, ...]])

    # If no auxiliary variable (such as forcings) is required, at each iteration
    # the input is equivalent to the output of the previous one.
    def _bypass(self, data=None, **kwargs):

        return data

    @property
    def _all_autoexecutable(self):

        # An auto-executable model can be trained without any wrapper class (as ModelMaker).
        # Basically is a method with does not use iterative optimization algorithms
        # (as certain classes of Reservoir Computing).
        all_autoexecutable = sum([hasattr(inst, 'fit') for inst in self.model_instances_list.values()])
        return all_autoexecutable == len(self.model_instances_list.keys())

    def set_parameters(self, parameters):
        # Serial dispatcher
        for model_id, model in self.model_instances_list.items():
            assert hasattr(model, 'set_parameters'), 'This model has not a parameters setting method.'
            model.set_parameters(parameters)

    # Serial fitting process
    def fit(self, input_data=None, target_data=None, auxiliary_data=None, index=None, shuffle=False):

        if auxiliary_data is not None:
            assert self.n_auxiliary == auxiliary_data.shape[1], f'auxiliary_data must have {self.n_auxiliary} columns'

        self._configure_data(input_data=input_data, target_data=target_data, auxiliary_data=auxiliary_data)

        # The configuration is done for all the models involved in the pool at a time
        # or individually if an index is provided.
        self._configure_model(model_config=self.model_config, index=index)

        sub_datasets, sub_datasets_target = self._get_sub_datasets(input_data=input_data,
                                                                   target_data=target_data,
                                                                   auxiliary_data=auxiliary_data)

        # It is used in case of models which depends on the history data
        if len(self.history_sizes) > 0:

            self.input_data_preparer = self._history_dependent_input
            self.train_input_data_preparer = self._train_history_dependent_input

            # Making each dataset proper to the correct history size
            sub_datasets = self._adequate_input_history(sub_datasets)

        else:
            pass

        msg = None

        if index is not None:
            assert self.template in self.independent_case, f"It is not possible " \
                                                           f"to independently execute the sub-model {index} " \
                                                           f"if the model pool is not independent_series."

            model_id = self._make_id(idx=index)
            sub_datasets_model_id = sub_datasets[model_id]
            sub_datasets_target_model_id = sub_datasets_target[model_id]

            self._single_sub_model_dispatcher(model_id, sub_datasets_model_id, sub_datasets_target_model_id, shuffle=shuffle)

        else:
            # Executing the fitting process for all the sub-models
            # contained in this pool
            msg = self._sub_model_dispatcher(sub_datasets, sub_datasets_target, shuffle=shuffle)

        print("Model configuration concluded.")

        return msg

    # Serial prediction
    def predict(self, initial_state=None, horizon=None, auxiliary_data=None, index=None, compare_data=None):

        assert len(initial_state.shape) >= 2, 'Input must have two dimensions at most.'

        if isinstance(auxiliary_data, np.ndarray):
            assert self.n_auxiliary == auxiliary_data.shape[1], f'auxiliary_data must have {self.n_auxiliary} columns'
            prepare_state = self._stack_auxiliary
            if horizon is None:
                horizon = auxiliary_data.shape[0]
        else:
            prepare_state = self._bypass
        if horizon is None:
            horizon = 1

        if index is not None:
            assert horizon == 1
            model_id = self._make_id(idx=index)
            simulate_instances = {model_id: self.model_instances_list[model_id]}
        else:
            simulate_instances = self.model_instances_list

        # These conditionals might be merged
        if self._all_autoexecutable:

            # Constructing the data to be used during the extrapolation
            initial_state_datasets = self._construct_subdatasets(self.groups_indices_input,
                                                                 initial_state,
                                                                 auxiliary_data=auxiliary_data[0:1])
            state_datasets = initial_state_datasets
            extrapolation_list = list()

            # Time extrapolation loop
            for step in range(horizon):

                step_outputs_list = list()

                # TODO It can be really parallelized in multiple
                #  independent processes running in dedicated
                #  computational nodes

                # Serial dispatcher
                for model_id, model in simulate_instances.items():

                    data = state_datasets.get(model_id)
                    out = model.step(data=data[0])
                    step_outputs_list.append(out)

                current_state_ = np.hstack(step_outputs_list)[None, :]
                extrapolation_list.append(current_state_)

                if compare_data is None:
                    print("Extrapolation step {} concluded".format(step))
                else:
                    if step < compare_data.shape[0]:
                        print("Extrapolation step {} concluded. Error {}".format(step, np.linalg.norm(current_state_-compare_data[step:step+1, ...])))

                if step >= horizon-1:
                    break

                state_datasets = self._construct_subdatasets(self.groups_indices_input,
                                                             current_state_,
                                                             auxiliary_data=auxiliary_data[step+1:step+2])

            return np.vstack(extrapolation_list)

        elif not self._all_autoexecutable and self.wrapper_model:

            initial_state_datasets = self._construct_subdatasets(self.groups_indices_input,
                                                                 initial_state)
            state_datasets = initial_state_datasets
            extrapolation_list = list()
            current_state = initial_state

            for step in range(horizon):

                step_outputs_list = list()

                for model_id, model in simulate_instances.items():

                    data = state_datasets.get(model_id)
                    data = self.train_input_data_preparer(model_id=model_id, data=data)
                    out = model.step(data=data)
                    step_outputs_list.append(out)

                    print('Step {}, sub-model {}'.format(step, model_id))

                current_state_ = np.concatenate(step_outputs_list, axis=-1)
                current_state = prepare_state(previous_data=current_state,
                                              data=current_state_,
                                              step=step)

                state_datasets = self._construct_subdatasets(self.groups_indices_input,
                                                             current_state)

                # Removing the horizon dimension (no longer necessary)
                extrapolation_list.append(current_state_[:, 0, ...])
                print("Extrapolation step {} concluded".format(step))

            return np.vstack(extrapolation_list)

        else:

            raise Exception("There is something wrong: a wrapper model (ModelMaker)"
                            "is necessary.")

    def reset(self):

        for model_id, model in self.model_instances_list.items():

            assert hasattr(model, 'reset'), f"The moodel {model} has no method reset."

            print(f'Resetting model {model_id}')
            model.reset()

    def set_reference(self, reference=None):

        for model_id, model in self.model_instances_list.items():

            assert hasattr(model, 'current_state'), f"The moodel {model} cannot set reference."

            print(f'Setting up reference state for the model {model_id}')
            model.set_reference(reference=reference)

    def save(self, path=None):
        try:
            with open(path, 'wb') as fp:
                pickle.dump(self, fp, protocol=4)
        except Exception as e:
            print(e, e.args)
            raise Exception(f"The object {self} is not serializable.")

    def save_pool_config(self, path=None):
        with open(path, 'w') as fp:
            json.dump(self.config_pool, fp, indent=6)

    def save_model(self, path: str, model_id: str):
        return self.model_instances_list[model_id].save(save_path=path, model_name=model_id)

    def load_model(self, path: str, model_id: str, index: int = None) -> None:
        regression = self._get_regression_class()
        model = regression.restore(path, model_id)
        self.load_model_instance(model, model_id, index)

    def load_model_instance(self, model, model_id=None, index: int = None) -> None:
        if index is None:
            model_id_new = model_id
        else:
            model_id_new = self._make_id(index)
        self.model_instances_list[model_id_new] = model

    def get_model_instance(self, model_id=None, index: int = None):
        if index is None:
            model_id_new = model_id
        else:
            model_id_new = self._make_id(index)
        return self.model_instances_list[model_id_new]

