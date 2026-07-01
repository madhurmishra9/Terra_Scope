# .tflint.hcl — provider-aware linting for TerraScope-generated modules.
# `tflint --init` installs these plugins (network required, best-effort).
# Bump versions as upstream releases; a failed --init falls back to core rules.

config {
  call_module_type = "local"
  force            = false
}

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

plugin "google" {
  enabled = true
  version = "0.31.0"
  source  = "github.com/terraform-linters/tflint-ruleset-google"
}

plugin "aws" {
  enabled = true
  version = "0.35.0"
  source  = "github.com/terraform-linters/tflint-ruleset-aws"
}

plugin "azurerm" {
  enabled = true
  version = "0.28.0"
  source  = "github.com/terraform-linters/tflint-ruleset-azurerm"
}
