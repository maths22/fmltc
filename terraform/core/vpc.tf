
resource "google_project_service" "vpcaccess-api" {
  project = var.project_id
  service = "vpcaccess.googleapis.com"
}

module "ml-redis-vpc-module" {
  source       = "terraform-google-modules/network/google"
  version      = "~> 3.3.0"
  project_id   = var.project_id
  network_name = "ml-redis-vpc-dev"
  mtu          = 1460

  subnets = [
    {
      subnet_name   = "serverless-subnet"
      subnet_ip     = "10.10.10.0/28"
      subnet_region = "us-central1"
    }
  ]
}

module "serverless-connector" {
  source     = "terraform-google-modules/network/google//modules/vpc-serverless-connector-beta"
  project_id = var.project_id
  vpc_connectors = [{
    name        = "central-serverless"
    region      = "us-central1"
    subnet_name = module.ml-redis-vpc-module.subnets["us-central1/serverless-subnet"].name
    machine_type  = "e2-standard-4"
    min_throughput = 200
    max_throughput = 700
  }
    # Uncomment to specify an ip_cidr_range
    #   , {
    #     name          = "central-serverless2"
    #     region        = "us-central1"
    #     network       = module.test-vpc-module.network_name
    #     ip_cidr_range = "10.10.11.0/28"
    #     subnet_name   = null
    #     machine_type  = "e2-standard-4"
    #     min_instances = 2
    #     max_instances = 7 }
  ]
  depends_on = [google_project_service.vpcaccess-api]
}