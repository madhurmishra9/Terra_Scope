output "bucket_name" {
  description = "The name of the primary GCS bucket"
  value       = google_storage_bucket.main.name
}

output "bucket_url" {
  description = "The URL of the primary GCS bucket (gs://...)"
  value       = google_storage_bucket.main.url
}

output "bucket_self_link" {
  description = "The self_link of the primary GCS bucket"
  value       = google_storage_bucket.main.self_link
}

output "replica_bucket_names" {
  description = "Names of replica buckets (empty list if replica_locations is empty)"
  value       = [for b in google_storage_bucket.replica : b.name]
}
