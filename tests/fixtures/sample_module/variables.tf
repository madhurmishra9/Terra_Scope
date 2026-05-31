variable "name" {
  description = "Base name for the GCS bucket"
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,28}[a-z0-9]$", var.name))
    error_message = "name must be 4-30 lowercase alphanumeric characters or hyphens."
  }
}

variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region for the bucket"
  type        = string
  default     = "us-central1"
}

variable "environment" {
  description = "Deployment environment"
  type        = string
  default     = "dev"
  validation {
    condition     = contains(["dev", "staging", "production"], var.environment)
    error_message = "environment must be dev, staging, or production."
  }
}

variable "enable_versioning" {
  description = "Enable object versioning on the bucket"
  type        = bool
  default     = false
}

variable "replica_locations" {
  description = "List of additional regions to replicate the bucket to (empty list = no replicas)"
  type        = list(string)
  default     = []
}

variable "kms_key_name" {
  description = "KMS key name for CMEK encryption (null = Google-managed encryption)"
  type        = string
  default     = null
  sensitive   = false
}

variable "cors_origins" {
  description = "List of CORS origins (null = no CORS configuration)"
  type        = list(string)
  default     = null
}

variable "additional_labels" {
  description = "Additional labels to apply to all resources"
  type        = map(string)
  default     = {}
}
