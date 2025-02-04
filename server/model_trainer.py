# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

__author__ = "lizlooney@google.com (Liz Looney)"

# Python Standard Library
from datetime import datetime, timedelta, timezone
import json
import logging
import math
import os
import time
import traceback

# Other Modules
from google.oauth2 import service_account
import googleapiclient.discovery
import tensorflow as tf
from tensorflow.core.util import event_pb2

# My Modules
import action
import blob_storage
import constants
import exceptions
import storage
import tflite_creator
import util

BUCKET = ('%s' % constants.PROJECT_ID)

STARTING_MODELS = {
    'SSD MobileNet v2 320x320': {
        'pipeline_config': 'tf2/20200711/ssd_mobilenet_v2_320x320_coco17_tpu-8/pipeline.config',
        'checkpoint': 'tf2/20200711/ssd_mobilenet_v2_320x320_coco17_tpu-8/checkpoint/ckpt-0',
    },
    'SSD MobileNet V2 FPNLite 320x320': {
        'pipeline_config': 'tf2/20200711/ssd_mobilenet_v2_fpnlite_320x320_coco17_tpu-8/pipeline.config',
        'checkpoint': 'tf2/20200711/ssd_mobilenet_v2_fpnlite_320x320_coco17_tpu-8/checkpoint/ckpt-0',
    },
    'SSD MobileNet V1 FPN 640x640': {
        'pipeline_config': 'tf2/20200711/ssd_mobilenet_v1_fpn_640x640_coco17_tpu-8/pipeline.config',
        'checkpoint': 'tf2/20200711/ssd_mobilenet_v1_fpn_640x640_coco17_tpu-8/checkpoint/ckpt-0',
    },
    'SSD MobileNet V2 FPNLite 640x640': {
        'pipeline_config': 'tf2/20200711/ssd_mobilenet_v2_fpnlite_640x640_coco17_tpu-8/pipeline.config',
        'checkpoint': 'tf2/20200711/ssd_mobilenet_v2_fpnlite_640x640_coco17_tpu-8/checkpoint/ckpt-0',
    },
}

def validate_starting_model(s):
    if s not in STARTING_MODELS:
        try:
            return storage.validate_uuid(s)
        except:
            message = "Error: '%s is not a valid argument." % s
            logging.critical(message)
            raise exceptions.HttpErrorBadRequest(message)
    return s


def get_starting_model_names():
    names = list(STARTING_MODELS.keys())
    return names

def start_training_model(team_uuid, description, dataset_uuids_json,
        starting_model, max_running_minutes, num_training_steps, create_time_ms):
    # Call retrieve_model_list to update all models (which may have finished training) and update
    # the team_entity.
    model_entities = retrieve_model_list(team_uuid)

    found_starting_model = starting_model in STARTING_MODELS
    if found_starting_model:
        starting_model_uuid = None
        starting_model_entity = None
        original_starting_model = starting_model
        user_visible_starting_model = starting_model
        starting_model_data = STARTING_MODELS[starting_model]
        config_template_blob_name = 'static/training/models/%s' % (
            starting_model_data['pipeline_config'])
        fine_tune_checkpoint = 'gs://%s/static/training/models/%s' % (
            BUCKET, starting_model_data['checkpoint'])
    else:
        # starting_model is the model_uuid of one of the user's own models.
        starting_model_uuid = starting_model
        # retrieve_model_entity will raise HttpErrorNotFound
        # if the team_uuid/starting_model_uuid is not found
        starting_model_entity = retrieve_model_entity(team_uuid, starting_model_uuid)
        if starting_model_entity['trained_checkpoint_path'] == '':
            message = 'Error: Trained checkpoint not found for model_uuid=%s.' % starting_model_uuid
            logging.critical(message)
            raise exceptions.HttpErrorNotFound(message)
        original_starting_model = starting_model_entity['original_starting_model']
        if original_starting_model not in STARTING_MODELS:
            message = 'Error: Original starting model is not longer supported: %s' % original_starting_model
            logging.critical(message)
            raise exceptions.HttpErrorNotFound(message)
        # user_visible_starting_model is the description field of the model entity.
        user_visible_starting_model = starting_model_entity['description']
        starting_model_data = STARTING_MODELS[original_starting_model]
        config_template_blob_name = 'static/training/models/%s' % (
            starting_model_data['pipeline_config'])
        fine_tune_checkpoint = starting_model_entity['trained_checkpoint_path']
        # Remove trailing .index if it's there.
        if fine_tune_checkpoint.endswith('.index'):
            fine_tune_checkpoint = fine_tune_checkpoint[:-6]

    dataset_uuid_list = json.loads(dataset_uuids_json)
    dataset_entities = storage.retrieve_dataset_entities(team_uuid, dataset_uuid_list)
    if len(dataset_entities) != len(dataset_uuid_list):
        message = 'Error: One or more datasets not found for dataset_uuids=%s.' % dataset_uuids_json
        logging.critical(message)
        raise exceptions.HttpErrorNotFound(message)

    previous_training_steps = 0
    dataset_uuids = []
    train_input_path = []
    eval_input_path = []
    train_frame_count = 0
    eval_frame_count = 0
    train_negative_frame_count = 0
    eval_negative_frame_count = 0
    train_dict_label_to_count = {}
    eval_dict_label_to_count = {}
    sorted_label_list = None
    label_map_path = None
    if starting_model_entity is not None:
        previous_training_steps = starting_model_entity['total_training_steps']
        dataset_uuids.extend(starting_model_entity['dataset_uuids'])
        train_input_path.extend(starting_model_entity['train_input_path'])
        eval_input_path.extend(starting_model_entity['eval_input_path'])
        train_frame_count += starting_model_entity['train_frame_count']
        eval_frame_count += starting_model_entity['eval_frame_count']
        train_negative_frame_count += starting_model_entity['train_negative_frame_count']
        eval_negative_frame_count += starting_model_entity['eval_negative_frame_count']
        util.extend_dict_label_to_count(train_dict_label_to_count, starting_model_entity['train_dict_label_to_count'])
        util.extend_dict_label_to_count(eval_dict_label_to_count, starting_model_entity['eval_dict_label_to_count'])
        sorted_label_list = starting_model_entity['sorted_label_list']
        label_map_path = starting_model_entity['label_map_path']

    for dataset_entity in dataset_entities:
        dataset_uuids.append(dataset_entity['dataset_uuid'])
        train_input_path.append(dataset_entity['train_input_path'])
        eval_input_path.append(dataset_entity['eval_input_path'])
        train_frame_count += dataset_entity['train_frame_count']
        eval_frame_count += dataset_entity['eval_frame_count']
        train_negative_frame_count += dataset_entity['train_negative_frame_count']
        eval_negative_frame_count += dataset_entity['eval_negative_frame_count']
        util.extend_dict_label_to_count(train_dict_label_to_count, dataset_entity['train_dict_label_to_count'])
        util.extend_dict_label_to_count(eval_dict_label_to_count, dataset_entity['eval_dict_label_to_count'])
        if sorted_label_list is None:
            sorted_label_list = dataset_entity['sorted_label_list']
            label_map_path = dataset_entity['label_map_path']
        elif sorted_label_list != dataset_entity['sorted_label_list']:
            message = "Error: The datasets contain different labels and cannot be used together."
            logging.critical(message)
            raise exceptions.HttpErrorBadRequest(message)

    # Create the pipeline.config file and store it in cloud storage.
    bucket = util.storage_client().get_bucket(BUCKET)
    pipeline_config = (bucket.blob(config_template_blob_name).download_as_string().decode('utf-8')
        .replace('TO_BE_CONFIGURED/eval_input_path', json.dumps(eval_input_path))
        .replace('TO_BE_CONFIGURED/fine_tune_checkpoint', fine_tune_checkpoint)
        .replace('TO_BE_CONFIGURED/label_map_path', label_map_path)
        .replace('TO_BE_CONFIGURED/num_classes', str(len(sorted_label_list)))
        .replace('TO_BE_CONFIGURED/num_examples', str(eval_frame_count))
        .replace('TO_BE_CONFIGURED/num_training_steps', str(num_training_steps))
        .replace('TO_BE_CONFIGURED/num_visualizations', str(min(100, eval_frame_count)))
        .replace('TO_BE_CONFIGURED/train_input_path',  json.dumps(train_input_path))
        .replace('TO_BE_CONFIGURED/warmup_steps_1000',  str(min(1000, num_training_steps)))
        .replace('TO_BE_CONFIGURED/warmup_steps_2000',  str(min(2000, num_training_steps)))
        )

    packageUris = [
        'gs://%s/static/training/object_detection-0.1_2.5.0.tar.gz' % BUCKET,
        'gs://%s/static/training/slim-0.1.tar.gz' % BUCKET,
        'gs://%s/static/training/pycocotools-2.0.tar.gz' % BUCKET,
    ]
    region = 'us-central1' # Choices are us-central1 or europe-west4
    runtimeVersion = '2.5' # Not supported beginning August 13, 2022
    pythonVersion = '3.7'

    # storage.model_trainer_starting will raise HttpErrorUnprocessableEntity
    # if the max_running_minutes exceeds the team's remaining_training_minutes.
    model_uuid = storage.model_trainer_starting(team_uuid, max_running_minutes)
    model_folder = blob_storage.get_model_folder(team_uuid, model_uuid)

    try:
        pipeline_config_path = blob_storage.store_pipeline_config(model_folder, pipeline_config)

        model_dir = blob_storage.get_model_folder_path(model_folder)
        job_dir = model_dir
        checkpoint_dir = model_dir

        train_job_id = __get_train_job_id(model_uuid)
        scheduling = {
            'maxRunningTime': '%ds' % int(max_running_minutes * 60),
        }
        train_training_input = {
            'scaleTier': 'BASIC_TPU',
            'packageUris': packageUris,
            'pythonModule': 'object_detection.model_main_tf2',
            'args': [
                '--model_dir', model_dir,
                '--pipeline_config_path', pipeline_config_path,
                '--checkpoint_every_n', str(100),
                '--use_tpu', 'true',
            ],
            'region': region,
            'jobDir': job_dir,
            'runtimeVersion': runtimeVersion,
            'pythonVersion': pythonVersion,
            'scheduling': scheduling,
        }
        train_job = {
            'jobId': train_job_id,
            'trainingInput': train_training_input
        }

        eval_job_id = __get_eval_job_id(model_uuid)
        eval_training_input = {
            'scaleTier': 'BASIC_GPU',
            'packageUris': packageUris,
            'pythonModule': 'object_detection.model_main_tf2',
            'args': [
                '--model_dir', model_dir,
                '--pipeline_config_path', pipeline_config_path,
                '--checkpoint_dir', checkpoint_dir,
            ],
            'region': region,
            'jobDir': job_dir,
            'runtimeVersion': runtimeVersion,
            'pythonVersion': pythonVersion,
        }
        eval_job = {
            'jobId': eval_job_id,
            'trainingInput': eval_training_input,
        }

        ml = __get_ml_service()
        parent = __get_parent()

        # Start the eval job first to because it runs slower than the train job. This helps it to
        # not miss the first checkpoint, but does not completely prevent that.
        try:
            if eval_frame_count > 0:
                eval_job_response = ml.projects().jobs().create(parent=parent, body=eval_job).execute()
            else:
                eval_job_response = None
        except:
            util.log('model_trainer.start_training_model - creating eval job - except %s' %
                traceback.format_exc().replace('\n', ' ... '))
            raise

        try:
            train_job_response = ml.projects().jobs().create(parent=parent, body=train_job).execute()
        except:
            util.log('model_trainer.start_training_model - creating training job - except %s' %
                traceback.format_exc().replace('\n', ' ... '))
            if eval_job_response is not None:
                # Cancel the eval job.
                ml.projects().jobs().cancel(name=__get_eval_job_name(model_uuid)).execute()
            raise

    except:
        # storage.failed_to_start_training will adjust the team's remaining training time and delete
        # any model blobs (such as the pipeline config file) that were created.
        storage.model_trainer_failed_to_start(team_uuid, model_folder, max_running_minutes)
        raise

    tensorflow_version = '2'
    model_entity = storage.model_trainer_started(team_uuid, model_uuid, description, model_folder,
        tensorflow_version, dataset_uuids, create_time_ms, max_running_minutes, num_training_steps,
        previous_training_steps, starting_model, user_visible_starting_model,
        original_starting_model, fine_tune_checkpoint,
        sorted_label_list, label_map_path, train_input_path, eval_input_path,
        train_frame_count, eval_frame_count, train_negative_frame_count, eval_negative_frame_count,
        train_dict_label_to_count, eval_dict_label_to_count, train_job_response, eval_job_response)
    __start_monitor_training(team_uuid, model_entity['model_uuid'])
    return model_entity

def retrieve_model_list(team_uuid):
    model_entities = storage.retrieve_model_list(team_uuid)
    ml = None
    for model_entity in model_entities:
        model_entity, ml = __update_model_entity_job_state(model_entity, ml)
    return model_entities

def retrieve_model_entity(team_uuid, model_uuid):
    # storage.retrieve_model_entity will raise HttpErrorNotFound
    # if the team_uuid/model_uuid is not found.
    model_entity = storage.retrieve_model_entity(team_uuid, model_uuid)
    model_entity, _ = __update_model_entity_job_state(model_entity)
    return model_entity

def __update_model_entity_job_state(model_entity, ml=None):
    # If the training and eval jobs weren't done last time we checked, check now.
    if is_not_done(model_entity):
        if ml is None:
            ml = __get_ml_service()
        train_job_name = __get_train_job_name(model_entity['model_uuid'])
        train_job_response = ml.projects().jobs().get(name=train_job_name).execute()
        if model_entity['eval_job']:
            eval_job_name = __get_eval_job_name(model_entity['model_uuid'])
            eval_job_response = ml.projects().jobs().get(name=eval_job_name).execute()
            if __is_alive(eval_job_response['state']):
                # If the training job has failed or been cancelled, cancel the eval job.
                if __is_dead_or_dying(train_job_response['state']):
                    ml.projects().jobs().cancel(name=eval_job_name).execute()
                    eval_job_response = ml.projects().jobs().get(name=eval_job_name).execute()
                # If the training job succeeded and we have the final eval, cancel the eval job.
                elif (__is_done(train_job_response['state']) and
                        model_entity['evaled_steps'] >= model_entity['trained_steps']):
                    ml.projects().jobs().cancel(name=eval_job_name).execute()
                    eval_job_response = ml.projects().jobs().get(name=eval_job_name).execute()
        else:
            eval_job_response = None
        model_entity = storage.update_model_entity_job_state(
            model_entity['team_uuid'], model_entity['model_uuid'], train_job_response, eval_job_response)
    return model_entity, ml

def is_not_done(model_entity):
    return (
        __is_not_done(model_entity['train_job_state']) or
        __is_not_done(model_entity['eval_job_state']))

def is_done(model_entity):
    return (
        __is_done(model_entity['train_job_state']) and
        __is_done(model_entity['eval_job_state']))

def cancel_training_model(team_uuid, model_uuid):
    # retrieve_model_entity will raise HttpErrorNotFound
    # if the team_uuid/model_uuid is not found.
    model_entity = retrieve_model_entity(team_uuid, model_uuid)
    ml = __get_ml_service()
    if __is_alive(model_entity['train_job_state']):
        try:
            train_job_name = __get_train_job_name(model_uuid)
            ml.projects().jobs().cancel(name=train_job_name).execute()
        except:
            util.log('model_trainer.cancel_training_model - canceling training job - except %s' %
                traceback.format_exc().replace('\n', ' ... '))
    if model_entity['eval_job']:
        if __is_alive(model_entity['eval_job_state']):
            try:
                eval_job_name = __get_eval_job_name(model_uuid)
                ml.projects().jobs().cancel(name=eval_job_name).execute()
            except:
                util.log('model_trainer.cancel_training_model - canceling eval job - except %s' %
                    traceback.format_exc().replace('\n', ' ... '))
    return storage.cancel_training_requested(team_uuid, model_uuid)

def __get_ml_service():
    scopes = ['https://www.googleapis.com/auth/cloud-platform']
    credentials = service_account.Credentials.from_service_account_file('key.json', scopes=scopes)
    return googleapiclient.discovery.build(
        serviceName='ml', version='v1', credentials=credentials, cache_discovery=False)

def __get_parent():
    # TODO(lizlooney): Is the project id here supposed to be our Google Cloud Project ID?
    return 'projects/%s' % constants.PROJECT_ID

def __get_train_job_id(model_uuid):
    return 'train_%s' % model_uuid

def __get_eval_job_id(model_uuid):
    return 'eval_%s' % model_uuid

def __get_train_job_name(model_uuid):
    return '%s/jobs/%s' % (__get_parent(), __get_train_job_id(model_uuid))

def __get_eval_job_name(model_uuid):
    return '%s/jobs/%s' % (__get_parent(), __get_eval_job_id(model_uuid))

def __is_alive(state):
    return (state == 'QUEUED' or
            state == 'PREPARING' or
            state == 'RUNNING')

def __is_dead_or_dying(state):
    return (state == 'FAILED' or
            state == 'CANCELLING' or
            state == 'CANCELLED')

def __is_not_done(state):
    return (state != '' and
            state != 'SUCCEEDED' and
            state != 'FAILED' and
            state != 'CANCELLED')

def __is_done(state):
    return not __is_not_done(state)

def maybe_restart_monitor_training(team_uuid, model_uuid):
    # retrieve_model_entity will raise HttpErrorNotFound
    # if the team_uuid/model_uuid is not found.
    model_entity = retrieve_model_entity(team_uuid, model_uuid)

    if ('monitor_training_finished' not in model_entity or
            'monitor_training_triggered_time_ms' not in model_entity or
            'monitor_training_active_time_ms' not in model_entity):
        # Some models were trained before these fields were added.
        return False, model_entity

    if model_entity['monitor_training_finished']:
        # The monitor training action finished.
        return False, model_entity

    if model_entity['monitor_training_triggered_time_ms'] != 0:
        # The monitor training action was not triggered yet. It shouldn't be restarted since it
        # hasn't even started the first time yet.
        return False, model_entity

    if model_entity['monitor_training_active_time_ms'] <= model_entity['monitor_training_triggered_time_ms']:
        # The monitor training action was triggered, but not active.
        # Check if it has been <= 3 minutes since it was triggered.
        # Since monitor_training_triggered_time_ms is non-zero, we can use the
        # monitor_training_triggered_time field.
        if datetime.now(timezone.utc) - model_entity['monitor_training_triggered_time'] <= timedelta(minutes=3):
            return False, model_entity
    else:
        # The monitor training action was active after it was triggered.
        # Check if it has been <= 3 minutes since it was active.
        # Since monitor_training_triggered_time_ms and monitor_training_active_time_ms are both
        # non-zero, we can use the monitor_training_triggered_time and monitor_training_active_time
        # fields.
        if datetime.now(timezone.utc) - model_entity['monitor_training_active_time'] <= timedelta(minutes=3):
            return False, model_entity

    return True, __start_monitor_training(team_uuid, model_uuid)

def __start_monitor_training(team_uuid, model_uuid):
    model_entity = storage.prepare_to_start_monitor_training(team_uuid, model_uuid)
    action_parameters = action.create_action_parameters(
        team_uuid, action.ACTION_NAME_MONITOR_TRAINING)
    action_parameters['team_uuid'] = team_uuid
    action_parameters['model_uuid'] = model_uuid
    action.trigger_action_via_blob(action_parameters)
    return model_entity

def monitor_training(action_parameters):
    team_uuid = action_parameters['team_uuid']
    model_uuid = action_parameters['model_uuid']

    model_entity = retrieve_model_entity(team_uuid, model_uuid)
    model_folder = model_entity['model_folder']
    prev_training_done = __is_done(model_entity['train_job_state'])

    while True:
        model_entity = storage.monitor_training_active(team_uuid, model_uuid)

        if not prev_training_done:
            training_done = __is_done(model_entity['train_job_state'])
            if training_done:
                # Training just finished. Trigger the action to create the tflite model.
                tflite_creator.trigger_create_tflite(team_uuid, model_uuid)
            prev_training_done = training_done

        previous_time_ms = model_entity['monitor_training_active_time_ms']

        for job_type in ['train', 'eval']:
            dict_path_to_updated = blob_storage.get_event_file_paths(model_folder, job_type)
            for event_file_path, updated in dict_path_to_updated.items():
                if ('dict_event_file_path_to_updated' in model_entity and
                        event_file_path in model_entity['dict_event_file_path_to_updated'] and
                        model_entity['dict_event_file_path_to_updated'][event_file_path] == updated):
                    continue
                largest_step, scalar_summary_items, image_summary_items = __monitor_training_for_event_file(
                    model_folder, job_type, event_file_path, action_parameters)
                model_entity = storage.update_model_entity_summary_items(team_uuid, model_uuid, job_type,
                    event_file_path, updated, largest_step, scalar_summary_items, image_summary_items)

        if is_done(model_entity):
            # If we didn't update the model during the for loop, we are done.
            if model_entity['monitor_training_active_time_ms'] == previous_time_ms:
                model_entity = storage.monitor_training_finished(team_uuid, model_uuid)
                return

        if action.remaining_timedelta(action_parameters) > timedelta(minutes=2):
            time.sleep(60)
        action.retrigger_if_necessary(action_parameters)


def __monitor_training_for_event_file(model_folder, job_type, event_file_path, action_parameters):
    largest_step = None
    scalar_summary_items = {}
    image_summary_items = {}
    for record in tf.data.TFRecordDataset(event_file_path):
        action.retrigger_if_necessary(action_parameters)
        event = event_pb2.Event.FromString(record.numpy())
        if not hasattr(event, 'step'):
            continue
        if largest_step is None or event.step > largest_step:
            largest_step = event.step
        if not hasattr(event, 'summary'):
            continue
        for value in event.summary.value:
            if (not hasattr(value, 'metadata') or
                    not hasattr(value.metadata, 'plugin_data') or
                    not hasattr(value.metadata.plugin_data, 'plugin_name')):
                continue
            if value.metadata.plugin_data.plugin_name == 'scalars':
                item_value = float(tf.make_ndarray(value.tensor))
                if math.isnan(item_value):
                    continue
                item = {
                    'step': event.step,
                    'tag': value.tag,
                    'value': item_value
                }
                scalar_summary_items[__make_key(event.step, value.tag)] = item
            elif value.metadata.plugin_data.plugin_name == 'images':
                if job_type == 'train':
                    # Don't bother saving training images.
                    continue
                image_value = tf.make_ndarray(value.tensor)
                if len(image_value) < 3: # width, height, image bytes
                    continue
                width = int(float(image_value[0].decode('utf-8')))
                height = int(float(image_value[1].decode('utf-8')))
                image_bytes = image_value[2]
                # There might be more than one image, but we only look at the first one.
                blob_storage.store_event_summary_image(model_folder, job_type,
                    event.step, value.tag, image_bytes)
                item = {
                    'job_type': job_type,
                    'step': event.step,
                    'tag': value.tag,
                    'width': width,
                    'height': height,
                }
                image_summary_items[__make_key(event.step, value.tag)] = item
    return largest_step, scalar_summary_items, image_summary_items

def __make_key(step, tag):
    return str(step) + '_' + tag


def retrieve_tags_and_steps(team_uuid, model_uuid, job_type, value_type):
    # retrieve_model_entity will raise HttpErrorNotFound
    # if the team_uuid/model_uuid is not found.
    model_entity = retrieve_model_entity(team_uuid, model_uuid)
    model_folder = model_entity['model_folder']
    summary_items_field_name = storage.get_model_entity_summary_items_field_name(job_type, value_type)
    if summary_items_field_name not in model_entity:
        return __retrieve_tags_and_steps_from_event_file(model_folder, job_type, value_type)
    step_and_tag_pairs = []
    for key, item in model_entity[summary_items_field_name].items():
        pair = {
            'step': item['step'],
            'tag': item['tag'],
        }
        step_and_tag_pairs.append(pair)
    return step_and_tag_pairs


def retrieve_summary_items(team_uuid, model_uuid, job_type, value_type, dict_step_to_tags):
    # retrieve_model_entity will raise HttpErrorNotFound
    # if the team_uuid/model_uuid is not found.
    model_entity = retrieve_model_entity(team_uuid, model_uuid)
    model_folder = model_entity['model_folder']
    summary_items_field_name = storage.get_model_entity_summary_items_field_name(job_type, value_type)
    if summary_items_field_name not in model_entity:
        return __retrieve_summary_items_from_event_file(model_folder, job_type, value_type, dict_step_to_tags)
    summary_items = []
    for step, tags in dict_step_to_tags.items():
        for tag in tags:
            item = model_entity[summary_items_field_name][__make_key(step, tag)]
            summary_item = {
                'step': item['step'],
                'tag': item['tag'],
            }
            if value_type == 'scalar':
                summary_item['value'] = item['value']
            elif value_type == 'image':
                exists, image_url = blob_storage.get_event_summary_image_download_url(
                    model_folder, item['job_type'], item['step'], item['tag'], None)
                if not exists:
                    continue
                summary_item['value'] = {
                    'width': item['width'],
                    'height': item['height'],
                    'image_url': image_url,
                }
            summary_items.append(summary_item)
    return summary_items


def __retrieve_tags_and_steps_from_event_file(model_folder, job_type, value_type):
    step_and_tag_pairs = []
    dict_path_to_updated = blob_storage.get_event_file_paths(model_folder, job_type)
    for event_file_path, updated in dict_path_to_updated.items():
        for record in tf.data.TFRecordDataset(event_file_path):
            event = event_pb2.Event.FromString(record.numpy())
            if (not hasattr(event, 'step') or
                    not hasattr(event, 'summary')):
                continue
            for value in event.summary.value:
                if (not hasattr(value, 'metadata') or
                        not hasattr(value.metadata, 'plugin_data') or
                        not hasattr(value.metadata.plugin_data, 'plugin_name')):
                    continue
                if value_type == 'scalar':
                    if value.metadata.plugin_data.plugin_name != 'scalars':
                        continue
                elif value_type == 'image':
                    if value.metadata.plugin_data.plugin_name != 'images':
                        continue
                else:
                    continue
                pair = {
                    'step': event.step,
                    'tag': value.tag,
                }
                step_and_tag_pairs.append(pair)
    return step_and_tag_pairs


def __retrieve_summary_items_from_event_file(model_folder, job_type, value_type, dict_step_to_tags):
    summary_items = []
    dict_path_to_updated = blob_storage.get_event_file_paths(model_folder, job_type)
    for event_file_path, updated in dict_path_to_updated.items():
        for record in tf.data.TFRecordDataset(event_file_path):
            event = event_pb2.Event.FromString(record.numpy())
            if (not hasattr(event, 'step') or
                    not hasattr(event, 'summary')):
                continue
            step_key = str(event.step)
            if step_key not in dict_step_to_tags:
                continue
            for value in event.summary.value:
                if (not hasattr(value, 'metadata') or
                        not hasattr(value.metadata, 'plugin_data') or
                        not hasattr(value.metadata.plugin_data, 'plugin_name')):
                    continue
                if value.tag not in dict_step_to_tags[step_key]:
                    continue
                if value_type == 'scalar':
                    if value.metadata.plugin_data.plugin_name != 'scalars':
                        continue
                elif value_type == 'image':
                    if value.metadata.plugin_data.plugin_name != 'images':
                        continue
                else:
                    continue
                summary_item = {
                    'step': event.step,
                    'tag': value.tag,
                }
                if value_type == 'scalar':
                    item_value = float(tf.make_ndarray(value.tensor))
                    if math.isnan(item_value):
                        continue
                    summary_item['value'] = item_value
                elif value_type == 'image':
                    image_value = tf.make_ndarray(value.tensor)
                    if len(image_value) < 3: # width, height, image bytes
                        continue
                    width = int(float(image_value[0].decode('utf-8')))
                    height = int(float(image_value[1].decode('utf-8')))
                    image_bytes = image_value[2]
                    exists, image_url = blob_storage.get_event_summary_image_download_url(
                        model_folder, job_type, event.step, value.tag, image_bytes)
                    if not exists:
                        continue
                    summary_item['value'] = {
                        'width': width,
                        'height': height,
                        'image_url': image_url,
                    }
                summary_items.append(summary_item)
    return summary_items
