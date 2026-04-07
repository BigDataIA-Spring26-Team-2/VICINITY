# ══════════════════════════════════════════════════════════════
# VICINITY — Snowflake Infrastructure
#
# 1 warehouse:   VICINITY_WH (all compute)
# 3 schemas:     RAW, USER_DATA, SCORECARDS
# 2 roles:       APP (CRUD), RAG_READONLY (SELECT only)
# ══════════════════════════════════════════════════════════════

resource "snowflake_database" "app" {
  name    = "VICINITY_${upper(var.environment)}"
  comment = "Vicinity student housing platform"
}

resource "snowflake_schema" "raw" {
  database   = snowflake_database.app.name
  name       = "RAW"
  comment    = "Source-of-truth ingested data"
  depends_on = [snowflake_database.app]
}

resource "snowflake_schema" "user_data" {
  database   = snowflake_database.app.name
  name       = "USER_DATA"
  comment    = "User state: profiles, bookmarks, routes"
  depends_on = [snowflake_database.app]
}

resource "snowflake_schema" "scorecards" {
  database   = snowflake_database.app.name
  name       = "SCORECARDS"
  comment    = "Pre-computed scores and denormalized read surfaces"
  depends_on = [snowflake_database.app]
}

resource "snowflake_warehouse" "app" {
  name                = "VICINITY_WH_${upper(var.environment)}"
  warehouse_size      = "X-SMALL"
  auto_suspend        = 60
  auto_resume         = true
  initially_suspended = true
  comment             = "All compute — ingestion, scoring, agent queries"
}

# ─── App Role (full CRUD) ────────────────────────────────────

resource "snowflake_account_role" "app" {
  name    = "VICINITY_APP_${upper(var.environment)}"
  comment = "Full CRUD for FastAPI and Airflow"
}

locals {
  schemas = {
    raw        = snowflake_schema.raw
    user_data  = snowflake_schema.user_data
    scorecards = snowflake_schema.scorecards
  }
}

resource "snowflake_grant_privileges_to_account_role" "app_wh" {
  account_role_name = snowflake_account_role.app.name
  privileges        = ["USAGE", "OPERATE"]
  on_account_object {
    object_type = "WAREHOUSE"
    object_name = snowflake_warehouse.app.name
  }
  depends_on = [snowflake_account_role.app, snowflake_warehouse.app]
}

resource "snowflake_grant_privileges_to_account_role" "app_db" {
  account_role_name = snowflake_account_role.app.name
  privileges        = ["USAGE"]
  on_account_object {
    object_type = "DATABASE"
    object_name = snowflake_database.app.name
  }
  depends_on = [snowflake_account_role.app, snowflake_database.app]
}

resource "snowflake_grant_privileges_to_account_role" "app_schema" {
  for_each          = local.schemas
  account_role_name = snowflake_account_role.app.name
  privileges        = ["USAGE", "CREATE TABLE", "CREATE VIEW"]
  on_schema {
    schema_name = "\"${snowflake_database.app.name}\".\"${each.value.name}\""
  }
  depends_on = [snowflake_account_role.app]
}

resource "snowflake_grant_privileges_to_account_role" "app_tables_all" {
  for_each          = local.schemas
  account_role_name = snowflake_account_role.app.name
  privileges        = ["SELECT", "INSERT", "UPDATE", "DELETE"]
  on_schema_object {
    all {
      object_type_plural = "TABLES"
      in_schema          = "\"${snowflake_database.app.name}\".\"${each.value.name}\""
    }
  }
  depends_on = [snowflake_account_role.app]
}

resource "snowflake_grant_privileges_to_account_role" "app_tables_future" {
  for_each          = local.schemas
  account_role_name = snowflake_account_role.app.name
  privileges        = ["SELECT", "INSERT", "UPDATE", "DELETE"]
  on_schema_object {
    future {
      object_type_plural = "TABLES"
      in_schema          = "\"${snowflake_database.app.name}\".\"${each.value.name}\""
    }
  }
  depends_on = [snowflake_account_role.app]
}

resource "snowflake_grant_privileges_to_account_role" "app_views_future" {
  for_each          = local.schemas
  account_role_name = snowflake_account_role.app.name
  privileges        = ["SELECT"]
  on_schema_object {
    future {
      object_type_plural = "VIEWS"
      in_schema          = "\"${snowflake_database.app.name}\".\"${each.value.name}\""
    }
  }
  depends_on = [snowflake_account_role.app]
}

# ─── App User ────────────────────────────────────────────────

resource "snowflake_user" "app" {
  name                 = "vicinity_user_${var.environment}"
  password             = var.app_user_password
  default_role         = snowflake_account_role.app.name
  default_warehouse    = snowflake_warehouse.app.name
  default_namespace    = "${snowflake_database.app.name}.RAW"
  must_change_password = false
  comment              = "Vicinity application service account"
  depends_on           = [snowflake_account_role.app, snowflake_warehouse.app, snowflake_database.app]
}

resource "snowflake_grant_account_role" "app_to_user" {
  role_name = snowflake_account_role.app.name
  user_name = snowflake_user.app.name
  depends_on = [
    snowflake_user.app,
    snowflake_account_role.app,
    snowflake_grant_privileges_to_account_role.app_wh,
    snowflake_grant_privileges_to_account_role.app_db
  ]
}