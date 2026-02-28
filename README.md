# CivicLens Lambda Functions

AWS Lambda Backend für Dokumentenverarbeitung und KI-Analyse.

## Funktionen

### create-document
Generiert Pre-Signed S3 Upload-URLs und initialisiert Dokument-Metadaten.

**Trigger**: Lambda Function URL  
**Runtime**: Python 3.9+

### extract-text
Extrahiert Text aus hochgeladenen PDFs.

**Trigger**: S3 Event (neues File in Raw Bucket)  
**Runtime**: Python 3.9+  
**Service**: AWS Textract

### structured-analysis
Analysiert extrahierten Text und generiert strukturierte Insights.

**Trigger**: S3 Event (Text-Extraktion abgeschlossen)  
**Runtime**: Python 3.9+  
**Service**: AWS Bedrock (Claude Sonnet)

### document-qa
Beantwortet Fragen zu analysierten Dokumenten mit Quellenangaben.

**Trigger**: Lambda Function URL  
**Runtime**: Python 3.9+  
**Service**: AWS Bedrock (Claude Haiku)

## Deployment

Alle Lambdas mit einem Befehl deployen:

```bash
./deploy.sh
```

Das Script:
- Erstellt ZIP-Dateien für alle Lambdas
- Inkludiert automatisch das `shared/` Verzeichnis
- Deployed zu AWS Lambda

### Voraussetzungen

- AWS CLI installiert und konfiguriert
- Lambda Functions in AWS erstellt
- Korrekte IAM Permissions

### Manuelle Konfiguration

Nach dem Deployment in der AWS Console konfigurieren:

**Environment Variables**:
- `RAW_BUCKET`: Name des Raw S3 Buckets
- `PROCESSED_BUCKET`: Name des Processed S3 Buckets

**Timeout**: 5 Minuten (für structured-analysis)

**Memory**: 512 MB (für structured-analysis)

## Projektstruktur

```
lambdas/
├── create-document/
│   ├── handler.py
│   └── requirements.txt
├── extract-text/
│   ├── handler.py
│   └── requirements.txt
├── structured-analysis/
│   ├── handler.py
│   └── requirements.txt
├── document-qa/
│   ├── handler.py
│   └── requirements.txt
├── shared/
│   ├── meta_utils.py      # Metadaten-Verwaltung
│   └── s3_utils.py        # S3-Hilfsfunktionen
├── tests/                 # Unit & Integration Tests
└── deploy.sh             # Deployment-Script
```

## Development

```bash
# Tests ausführen
python -m pytest tests/

# Logs anzeigen
aws logs tail /aws/lambda/civiclens-create-document --follow
aws logs tail /aws/lambda/civiclens-extract-text --follow
aws logs tail /aws/lambda/civiclens-structured-analysis --follow
aws logs tail /aws/lambda/civiclens-document-qa --follow
```

## IAM Permissions

Lambda Execution Role benötigt:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject"
      ],
      "Resource": [
        "arn:aws:s3:::civiclens-raw-bucket/*",
        "arn:aws:s3:::civiclens-processed-bucket/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "textract:DetectDocumentText"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel"
      ],
      "Resource": [
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-3-5-sonnet-20241022-v2:0",
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-3-5-haiku-20241022-v1:0"
      ]
    }
  ]
}
```

## Troubleshooting

**Import Errors**: ZIP-Struktur prüfen (shared/ muss im Root sein)  
**Permission Errors**: Lambda Execution Role prüfen  
**Timeout Errors**: Lambda Timeout erhöhen (5 Min für Analysis)

## Lizenz

MIT
