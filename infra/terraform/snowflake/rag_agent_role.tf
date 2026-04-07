resource "snowflake_account_role" "rag_readonly" {
  name    = "VICINITY_RAG_READONLY_${upper(var.environment)}"
  comment = "Read-only role for RAG agent SQL queries"
}

resource "snowflake_grant_privileges_to_account_role" "rag_wh" {
  account_role_name = snowflake_account_role.rag_readonly.name
  privileges        = ["USAGE"]
  on_account_object {
    object_type = "WAREHOUSE"
    object_name = snowflake_warehouse.app.name
  }
  depends_on = [
    snowflake_account_role.rag_readonly,
    snowflake_warehouse.app
  ]
}

resource "snowflake_grant_privileges_to_account_role" "rag_db" {
  account_role_name = snowflake_account_role.rag_readonly.name
  privileges        = ["USAGE"]
  on_account_object {
    object_type = "DATABASE"
    object_name = snowflake_database.app.name
  }
  depends_on = [
    snowflake_account_role.rag_readonly,
    snowflake_database.app
  ]
}

resource "snowflake_grant_privileges_to_account_role" "rag_schema" {
  for_each = local.schemas

  account_role_name = snowflake_account_role.rag_readonly.name
  privileges        = ["USAGE"]

  on_schema {
    schema_name = "\"${snowflake_database.app.name}\".\"${each.value.name}\""
  }

  depends_on = [snowflake_account_role.rag_readonly]
}

resource "snowflake_grant_privileges_to_account_role" "rag_select_all" {
  for_each = local.schemas

  account_role_name = snowflake_account_role.rag_readonly.name
  privileges        = ["SELECT"]

  on_schema_object {
    all {
      object_type_plural = "TABLES"
      in_schema          = "\"${snowflake_database.app.name}\".\"${each.value.name}\""
    }
  }

  depends_on = [snowflake_account_role.rag_readonly]
}

resource "snowflake_grant_privileges_to_account_role" "rag_select_future" {
  for_each = local.schemas

  account_role_name = snowflake_account_role.rag_readonly.name
  privileges        = ["SELECT"]

  on_schema_object {
    future {
      object_type_plural = "TABLES"
      in_schema          = "\"${snowflake_database.app.name}\".\"${each.value.name}\""
    }
  }

  depends_on = [snowflake_account_role.rag_readonly]
}

resource "snowflake_grant_privileges_to_account_role" "rag_views_future" {
  for_each = local.schemas

  account_role_name = snowflake_account_role.rag_readonly.name
  privileges        = ["SELECT"]

  on_schema_object {
    future {
      object_type_plural = "VIEWS"
      in_schema          = "\"${snowflake_database.app.name}\".\"${each.value.name}\""
    }
  }

  depends_on = [snowflake_account_role.rag_readonly]
}

resource "snowflake_grant_account_role" "rag_to_user" {
  role_name = snowflake_account_role.rag_readonly.name
  user_name = snowflake_user.app.name

  depends_on = [
    snowflake_user.app,
    snowflake_account_role.rag_readonly,
    snowflake_grant_privileges_to_account_role.rag_wh,
    snowflake_grant_privileges_to_account_role.rag_db
  ]
}