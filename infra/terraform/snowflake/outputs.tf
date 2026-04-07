output "database_name" {
  value = snowflake_database.app.name
}

output "schemas" {
  value = {
    raw        = snowflake_schema.raw.name
    user_data  = snowflake_schema.user_data.name
    scorecards = snowflake_schema.scorecards.name
  }
}

output "warehouse_name" {
  value = snowflake_warehouse.app.name
}

output "roles" {
  value = {
    app          = snowflake_account_role.app.name
    rag_readonly = snowflake_account_role.rag_readonly.name
  }
}

output "app_user" {
  value = snowflake_user.app.name
}

output "connection_string" {
  value = {
    account   = var.snowflake_account
    user      = snowflake_user.app.name
    database  = snowflake_database.app.name
    warehouse = snowflake_warehouse.app.name
    role_app  = snowflake_account_role.app.name
    role_rag  = snowflake_account_role.rag_readonly.name
  }
  sensitive = true
}