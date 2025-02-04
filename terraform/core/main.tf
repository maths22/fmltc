terraform {
  required_providers {
    google = {
      source = "hashicorp/google"
      version = "3.81.0"
    }
    google-ml = {
      source = "cmacfarl/google-ml"
      version = "0.1.0"
    }
  }
}

provider "google" {
  credentials = file(var.credentials_file)

  project = var.project_id
  region  = var.region
  zone    = var.zone
}

provider "google-ml" {
  credentials = var.credentials_file
  project     = var.project_id
}

variable "gcp_service_list" {
  description = "The list of apis necessary for fmltc"
  type = list(string)
  default = [
    "storage.googleapis.com",
    "cloudfunctions.googleapis.com",
    # Double check if datastore is necessary
    "datastore.googleapis.com",
    "firestore.googleapis.com",
    "ml.googleapis.com",
    "compute.googleapis.com",
    "cloudbuild.googleapis.com",
    "run.googleapis.com",
    "appengine.googleapis.com",
    "redis.googleapis.com",
    "serviceusage.googleapis.com"
  ]
}

resource "google_project_service" "first_dependency" {
  #
  # Cloud resource manager is necessary for terraform to enable the
  # rest of the APIs.  This is here because paranoia that terraform's
  # parallelism will break the apply if this isn't guaranteed to happen
  # first.
  #
  service = "cloudresourcemanager.googleapis.com"
  disable_dependent_services = true
}

resource "google_project_service" "gcp_services" {
  depends_on = [google_project_service.first_dependency]
  for_each = toset(var.gcp_service_list)
  service = each.key
  disable_dependent_services = true
}

data "archive_file" "cloud-function-src" {
  type        = "zip"
  source_dir  = "${path.root}/../../server"
  output_path = "${path.root}/../../generated/gcf-src.zip"
  excludes = [ "__pycache__", "static" ]
}

data "archive_file" "app-server-src" {
  type        = "zip"
  source_dir  = "${path.root}/../../server"
  output_path = "${path.root}/../../generated/gae-src.zip"
  excludes = [ "__pycache__", "static/training" ]
}

resource "google_storage_bucket_object" "cloud-function-archive" {
  name   = "${data.archive_file.cloud-function-src.output_md5}.zip"
  source = "${path.root}/../../generated/gcf-src.zip"
  bucket = google_storage_bucket.fmltc-gcf-source.name
}

resource "google_storage_bucket_object" "app-server-archive" {
  name   = "${data.archive_file.app-server-src.output_md5}.zip"
  source = "${path.root}/../../generated/gae-src.zip"
  bucket = google_storage_bucket.fmltc-gae-source.name
}

resource "google_cloudfunctions_function" "frame-extraction" {
  name        = "perform_action"
  description = "Extracts frames after a video upload"
  runtime     = "python39"

  available_memory_mb   = 8192
  timeout               = 540
  entry_point           = "perform_action"

  source_archive_bucket = google_storage_bucket.fmltc-gcf-source.name
  source_archive_object = google_storage_bucket_object.cloud-function-archive.name

  environment_variables = {
    project_id = var.project_id
  }

  timeouts {
    create = "60m"
    update = "60m"
  }

  event_trigger {
    event_type  = "google.storage.object.finalize"
    resource    = google_storage_bucket.fmltc-action-parameters.name
  }
}

#
# App engine setup
#
resource "google_app_engine_application" "fmltc-app" {
  project = var.project_id
  location_id = var.app_engine_region
  database_type = "CLOUD_FIRESTORE"
  depends_on = [google_project_service.gcp_services]
}

resource "google_app_engine_standard_app_version" "fmltc-app-v1" {
  runtime    = "python39"
  service    = "default"
  version_id = "v1"

  entrypoint {
    shell = "gunicorn -b :$PORT main:app"
  }

  automatic_scaling {
    max_concurrent_requests = 10
    min_idle_instances = 0
    max_idle_instances = 3
    min_pending_latency = "1s"
    max_pending_latency = "5s"
    standard_scheduler_settings {
      target_cpu_utilization = 0.5
      target_throughput_utilization = 0.75
      min_instances = 2
      max_instances = 10
    }
  }

  timeouts {
    create = "60m"
    update = "60m"
  }

  vpc_access_connector {
    name = "projects/${var.project_id}/locations/${var.region}/connectors/central-serverless"
  }
  deployment {
    zip {
      source_url = "https://storage.googleapis.com/${google_storage_bucket.fmltc-gae-source.name}/${google_storage_bucket_object.app-server-archive.name}"
    }
  }

  #
  # auth_fail_action, login, and security level will get assigned defaults by the API
  # if they are not defined here causing terraform to think your resource has
  # changed each time it runs a new plan.
  #
  #   https://github.com/hashicorp/terraform-provider-google/issues/9013
  #
  # Defaults from the API are
  #   auth_fail_action = "AUTH_FAIL_ACTION_REDIRECT"
  #   login            = "LOGIN_OPTIONAL"
  #   security_level   = "SECURE_OPTIONAL"
  #
  handlers {
    auth_fail_action = "AUTH_FAIL_ACTION_REDIRECT"
    login            = "LOGIN_OPTIONAL"
    security_level   = "SECURE_OPTIONAL"
    url_regex = "/favicon.ico"
    static_files {
      path = "static/favicon.ico"
      upload_path_regex = "static/favicon.ico"
      expiration = "86400s"
    }
  }

  handlers {
    url_regex = "/.*"
    auth_fail_action = "AUTH_FAIL_ACTION_REDIRECT"
    login            = "LOGIN_OPTIONAL"
    security_level = "SECURE_ALWAYS"
    script {
      script_path = "auto"
    }
    redirect_http_response_code = "REDIRECT_HTTP_RESPONSE_CODE_301"
  }

  #
  # Again see:
  #   https://github.com/hashicorp/terraform-provider-google/issues/9013
  # The API automatically adds this entire handler.
  #
  handlers {
    auth_fail_action = "AUTH_FAIL_ACTION_REDIRECT"
    login            = "LOGIN_OPTIONAL"
    security_level   = "SECURE_OPTIONAL"
    url_regex = ".*"
    script {
      script_path = "auto"
    }
  }

  instance_class = "F4"

  env_variables = {
    PROJECT_ID = var.project_id
    ORIGIN = var.project_url
    USE_OIDC = "true"
    REDIS_IP_ADDR = google_redis_instance.ml-redis-dev.host
  }

  depends_on = [module.serverless-connector]
}


data "ml_config" "cfg" {
  provider = google-ml
  depends_on = [google_project_service.gcp_services]
}

resource "google_project_iam_binding" "tpu_role" {
  project = var.project_id
  role = "roles/ml.serviceAgent"
  members = [
    "serviceAccount:${data.ml_config.cfg.tpu_service_account}",
    "serviceAccount:${data.ml_config.cfg.service_account}"
  ]
}

