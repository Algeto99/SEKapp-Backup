#!/usr/bin/env bash

# deploy_env.sh
# Automation script to provision a complete isolated environment in Google Cloud Platform (GCP)
# for a new client or deployment stage.
#
# Usage:
#   ./deploy_env.sh -p <PROJECT_ID> -n <ENV_NAME> -r <REGION> -d <DB_TIER>

set -euo pipefail

# Configurable defaults
PROJECT_ID=""
ENV_NAME=""
REGION="us-central1"
DB_TIER="db-f1-micro" # db-f1-micro is for development. Use db-custom-1-3840 for Production.

# Help message
show_help() {
    echo "Usage: ./deploy_env.sh [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  -p, --project      GCP Project ID (Required)"
    echo "  -n, --name         Environment name (e.g., sesursa, dev, prod) (Required)"
    echo "  -r, --region       GCP Region (Default: us-central1)"
    echo "  -t, --tier         Cloud SQL Tier (Default: db-f1-micro)"
    echo "  -h, --help         Show this help message"
    echo ""
    exit 0
}

# Parse command line options
while [[ $# -gt 0 ]]; do
    key="$1"
    case $key in
        -p|--project)
            PROJECT_ID="$2"
            shift 2
            ;;
        -n|--name)
            ENV_NAME="$(echo "$2" | tr '[:upper:]' '[:lower:]')"
            shift 2
            ;;
        -r|--region)
            REGION="$2"
            shift 2
            ;;
        -t|--tier)
            DB_TIER="$2"
            shift 2
            ;;
        -h|--help)
            show_help
            ;;
        *)
            echo "Unknown option: $1"
            show_help
            ;;
    esac
done

# Validation
if [[ -z "$PROJECT_ID" || -z "$ENV_NAME" ]]; then
    echo "Error: GCP Project ID (-p) and Environment Name (-n) are required."
    show_help
fi

# Resource Names
VPC_NAME="secapp-vpc-${ENV_NAME}"
CONNECTOR_NAME="vpc-conn-${ENV_NAME}"
DB_INSTANCE_NAME="db-${ENV_NAME}"
DB_USER="app_user_${ENV_NAME}"
DB_NAME="secapp_db_${ENV_NAME}"
SERVICE_NAME="secapp-${ENV_NAME}"
SERVICE_ACCOUNT_NAME="sa-${ENV_NAME}"

echo "================================================================="
echo " Starting GCP Provisioning for Environment: '${ENV_NAME}'"
echo " Target Project: ${PROJECT_ID}"
echo " Region: ${REGION}"
echo " DB Tier: ${DB_TIER}"
echo "================================================================="

# Set active project
gcloud config set project "$PROJECT_ID"

echo "1. Enabling required GCP APIs..."
gcloud services enable \
    run.googleapis.com \
    sqladmin.googleapis.com \
    secretmanager.googleapis.com \
    vpcaccess.googleapis.com \
    compute.googleapis.com \
    servicenetworking.googleapis.com

echo "2. Setting up VPC Network..."
if ! gcloud compute networks describe "$VPC_NAME" &>/dev/null; then
    gcloud compute networks create "$VPC_NAME" --subnet-mode=auto
    echo "VPC network '$VPC_NAME' created."
else
    echo "VPC network '$VPC_NAME' already exists. Skipping."
fi

echo "3. Reserving private connection IP range for Cloud SQL Peering..."
PEERING_RANGE_NAME="google-managed-services-${VPC_NAME}"
if ! gcloud compute addresses describe "$PEERING_RANGE_NAME" --global &>/dev/null; then
    gcloud compute addresses create "$PEERING_RANGE_NAME" \
        --global \
        --purpose=VPC_PEERING \
        --addresses=10.20.0.0 \
        --prefix-length=16 \
        --network="$VPC_NAME"
    echo "IP range reserved."
else
    echo "IP range already reserved. Skipping."
fi

echo "4. Establishing Private Services Access VPC Peering..."
gcloud services vpc-peerings connect \
    --service=servicenetworking.googleapis.com \
    --ranges="$PEERING_RANGE_NAME" \
    --network="$VPC_NAME"

echo "5. Creating Serverless VPC Access Connector..."
if ! gcloud compute networks vpc-access connectors describe "$CONNECTOR_NAME" --region="$REGION" &>/dev/null; then
    gcloud compute networks vpc-access connectors create "$CONNECTOR_NAME" \
        --region="$REGION" \
        --network="$VPC_NAME" \
        --range=10.30.0.0/28
    echo "VPC connector '$CONNECTOR_NAME' created."
else
    echo "VPC connector already exists. Skipping."
fi

echo "6. Provisioning private Cloud SQL PostgreSQL 14 instance..."
if ! gcloud sql instances describe "$DB_INSTANCE_NAME" &>/dev/null; then
    gcloud beta sql instances create "$DB_INSTANCE_NAME" \
        --database-version=POSTGRES_14 \
        --region="$REGION" \
        --network="projects/${PROJECT_ID}/global/networks/${VPC_NAME}" \
        --no-assign-ip \
        --tier="$DB_TIER"
    echo "Cloud SQL instance '$DB_INSTANCE_NAME' created."
else
    echo "Cloud SQL instance already exists. Skipping."
fi

# Retrieve DB private IP
DB_PRIVATE_IP=$(gcloud sql instances describe "$DB_INSTANCE_NAME" --format="value(ipAddresses[0].ipAddress)")
echo "Database Private IP: ${DB_PRIVATE_IP}"

echo "7. Creating database and user..."
gcloud sql databases create "$DB_NAME" --instance="$DB_INSTANCE_NAME" || true

# Generate secure database password
DB_PASS=$(openssl rand -hex 24)
gcloud sql users create "$DB_USER" --instance="$DB_INSTANCE_NAME" --password="$DB_PASS" || true

echo "8. Provisioning secrets in Secret Manager..."
# Helper function to create a secret and add a version
create_secret_if_missing() {
    local secret_name="$1"
    local secret_value="$2"
    if ! gcloud secrets describe "$secret_name" &>/dev/null; then
        gcloud secrets create "$secret_name" --replication-policy="automatic"
        echo -n "$secret_value" | gcloud secrets versions add "$secret_name" --data-file=-
        echo "Secret '$secret_name' created."
    else
        echo "Secret '$secret_name' already exists. Skipping."
    fi
}

DATABASE_URL="postgresql://${DB_USER}:${DB_PASS}@${DB_PRIVATE_IP}/${DB_NAME}"
create_secret_if_missing "DATABASE_URL_${ENV_NAME}" "$DATABASE_URL"

# Generate encryption keys
FLASK_KEY=$(openssl rand -hex 32)
create_secret_if_missing "FLASK_SECRET_KEY_${ENV_NAME}" "$FLASK_KEY"

JWT_KEY=$(openssl rand -hex 32)
create_secret_if_missing "JWT_SECRET_KEY_${ENV_NAME}" "$JWT_KEY"

# Temporary SMTP credentials template
create_secret_if_missing "EMAIL_PASSWORD_SECRET_${ENV_NAME}" "CHANGE_ME"

echo "9. Creating Custom Service Account..."
SA_EMAIL="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
if ! gcloud iam service-accounts describe "$SA_EMAIL" &>/dev/null; then
    gcloud iam service-accounts create "$SERVICE_ACCOUNT_NAME" \
        --display-name="Service Account for SecApp ${ENV_NAME}"
    echo "Service account created."
else
    echo "Service account already exists."
fi

# Grant necessary permissions
gcloud secrets add-iam-policy-binding "DATABASE_URL_${ENV_NAME}" --member="serviceAccount:${SA_EMAIL}" --role="roles/secretmanager.secretAccessor"
gcloud secrets add-iam-policy-binding "FLASK_SECRET_KEY_${ENV_NAME}" --member="serviceAccount:${SA_EMAIL}" --role="roles/secretmanager.secretAccessor"
gcloud secrets add-iam-policy-binding "JWT_SECRET_KEY_${ENV_NAME}" --member="serviceAccount:${SA_EMAIL}" --role="roles/secretmanager.secretAccessor"
gcloud secrets add-iam-policy-binding "EMAIL_PASSWORD_SECRET_${ENV_NAME}" --member="serviceAccount:${SA_EMAIL}" --role="roles/secretmanager.secretAccessor"

# SQL Instance connection name
CONNECTION_NAME="${PROJECT_ID}:${REGION}:${DB_INSTANCE_NAME}"

echo "10. Compiling and building docker container image via Cloud Build..."
# Note: In a production pipeline, this image tag would match release commits.
IMAGE_TAG="gcr.io/${PROJECT_ID}/new-secapp:${ENV_NAME}"
gcloud builds submit --tag "$IMAGE_TAG" "$(dirname "$0")/../monolith"

echo "11. Deploying application to Cloud Run..."
gcloud run deploy "$SERVICE_NAME" \
    --image="$IMAGE_TAG" \
    --region="$REGION" \
    --vpc-connector="projects/${PROJECT_ID}/locations/${REGION}/connectors/${CONNECTOR_NAME}" \
    --vpc-egress=private-ranges-only \
    --add-cloudsql-instances="$CONNECTION_NAME" \
    --service-account="$SA_EMAIL" \
    --set-env-vars="GCP_PROJECT_ID=${PROJECT_ID},ADMIN_EMAIL=admin@${ENV_NAME}.com,EMAIL_USERNAME=no-reply@${ENV_NAME}.com" \
    --set-secrets="DATABASE_URL=DATABASE_URL_${ENV_NAME}:latest,EMAIL_PASSWORD_SECRET=EMAIL_PASSWORD_SECRET_${ENV_NAME}:latest,FLASK_SECRET_KEY=FLASK_SECRET_KEY_${ENV_NAME}:latest,JWT_SECRET_KEY=JWT_SECRET_KEY_${ENV_NAME}:latest" \
    --allow-unauthenticated

SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" --region="$REGION" --format="value(status.url)")

echo "================================================================="
echo " Environment '${ENV_NAME}' provisioned successfully!"
echo " Service URL: ${SERVICE_URL}"
echo ""
echo "Next Steps:"
echo "1. Run the database initialization DDL using 'sql/schema.sql'"
echo "2. Run onboarding scripts or templates using 'sql/onboard_tenant_template.sql'"
echo "3. Configure DNS mappings and SSL certificates if custom domains are used."
echo "================================================================="
