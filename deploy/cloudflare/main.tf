# The public name of the production app. Cloudflare proxies it, so visitors get Cloudflare's
# certificate and never see the origin IP: the public IP of the AKS app routing NGINX
# (deploy/azure/provision.sh, deploy/overlays/production/ingress.yaml).
#
#   export CLOUDFLARE_API_TOKEN=...   # Zone:Read and DNS:Edit on isaacon.dev
#   terraform -chdir=deploy/cloudflare init
#   terraform -chdir=deploy/cloudflare apply -var origin_ip=$(kubectl get service nginx \
#     -n app-routing-system -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
terraform {
  required_version = ">= 1.6"
  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.0"
    }
  }
}

provider "cloudflare" {} # reads CLOUDFLARE_API_TOKEN

variable "zone" {
  type    = string
  default = "isaacon.dev"
}

variable "hostname" {
  type    = string
  default = "insight.isaacon.dev"
}

variable "origin_ip" {
  description = "Public IP of the production cluster's app routing NGINX service"
  type        = string
  validation {
    condition     = can(cidrhost("${var.origin_ip}/32", 0))
    error_message = "origin_ip must be an IPv4 address."
  }
}

data "cloudflare_zone" "this" {
  filter = { name = var.zone }
}

resource "cloudflare_dns_record" "insight" {
  zone_id = data.cloudflare_zone.this.zone_id
  name    = var.hostname
  type    = "A"
  content = var.origin_ip
  proxied = true
  ttl     = 1 # automatic, the only value a proxied record takes
  comment = "production AKS app routing NGINX, managed by deploy/cloudflare"
}

output "url" {
  value = "https://${cloudflare_dns_record.insight.name}"
}
