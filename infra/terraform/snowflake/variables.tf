variable "snowflake_account" {
  type = string
}

variable "snowflake_user" {
  type = string
}

variable "snowflake_password" {
  type      = string
  sensitive = true
}

variable "snowflake_role" {
  type    = string
  default = "ACCOUNTADMIN"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "app_user_password" {
  type      = string
  sensitive = true
}
