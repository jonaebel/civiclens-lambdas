#!/bin/bash

# CivicLens Lambda ZIP Script
# Creates  ZIP file for all Lambda (inklusive the /Shared files ) -> ready to deploy

set -e

# Colors (for the loooks)
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}CivicLens Lambda ZIP Creation${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# Lambda function names (dir)
LAMBDAS=(
    "create-document"
    "extract-text"
    "structured-analysis"
    "document-qa"
)

# ZIP creation
create_zip() {
    local dir=$1
    local zip_name="${dir}.zip"

    echo -e "${BLUE}Creating ${zip_name}...${NC}"

    # rmv old ZIP
    rm -f "${dir}/${zip_name}"

    # check if dependencies and requirements exists and install them
    if [ -f "${dir}/requirements.txt" ]; then
        echo -e "${BLUE}Installing dependencies from requirements.txt...${NC}"
        python3 -m pip install -r "${dir}/requirements.txt" -t "${dir}/package/" --platform manylinux2014_x86_64 --only-binary=:all: || {
            echo -e "${RED}Warning: Some packages may need manual installation${NC}"
            python3 -m pip install -r "${dir}/requirements.txt" -t "${dir}/package/"
        }

        # ZIP creation including dependencies
        cd "${dir}/package"
        zip -r "../${zip_name}" .
        cd ../..

        # adding handler and __init__.py
        cd "${dir}"
        zip -g "${zip_name}" handler.py __init__.py
        cd ..
    else
        # no no  requirements.txt - only zip the handler
        cd "$dir"
        zip -r "$zip_name" handler.py __init__.py
        cd ..
    fi

    # adding shared module
    cd shared
    zip -r "../${dir}/${zip_name}" __init__.py *.py
    cd ..

    echo -e "${GREEN}✓ ${zip_name} created ($(du -h "${dir}/${zip_name}" | cut -f1))${NC}"

    # cleanup package directory
    if [ -d "${dir}/package" ]; then
        rm -rf "${dir}/package"
    fi
}

# ZIP file creation
echo -e "${BLUE}Creating ZIP . . .${NC}"
echo ""

for lambda in "${LAMBDAS[@]}"; do
    create_zip "$lambda"
    echo ""
done

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}All ZIP files created !${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "ZIP files:"
for lambda in "${LAMBDAS[@]}"; do
    echo "  ${lambda}/${lambda}.zip"
done
