locals {
  name_prefix = "${var.environment}-${var.name}"
  common_labels = merge(
    {
      environment = var.environment
      managed_by  = "terraform"
      module      = "sample-gcs"
    },
    var.additional_labels,
  )
}

resource "google_storage_bucket" "main" {
  name          = local.name_prefix
  location      = var.region
  project       = var.project_id
  force_destroy = false

  labels = local.common_labels

  public_access_prevention    = "enforced"
  uniform_bucket_level_access = true

  # Feature gate: versioning (dynamic block keyed on enable_versioning)
  dynamic "versioning" {
    for_each = var.enable_versioning ? [1] : []
    content {
      enabled = true
    }
  }

  # Feature gate: CMEK encryption (count keyed on kms_key_name)
  dynamic "encryption" {
    for_each = var.kms_key_name != null ? [var.kms_key_name] : []
    content {
      default_kms_key_name = encryption.value
    }
  }

  # Feature gate: CORS (dynamic block keyed on cors_origins)
  dynamic "cors" {
    for_each = var.cors_origins != null ? [var.cors_origins] : []
    content {
      origin          = cors.value
      method          = ["GET", "HEAD"]
      response_header = ["Content-Type"]
      max_age_seconds = 3600
    }
  }
}

# Feature gate: replica buckets (count keyed on replica_locations)
resource "google_storage_bucket" "replica" {
  count = length(var.replica_locations)

  name     = "${local.name_prefix}-replica-${count.index}"
  location = var.replica_locations[count.index]
  project  = var.project_id

  labels                      = local.common_labels
  public_access_prevention    = "enforced"
  uniform_bucket_level_access = true
}
