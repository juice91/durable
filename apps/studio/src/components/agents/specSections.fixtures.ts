/** Test fixtures: the processor catalog exactly as `GET /api/v1/processors` serves it (generated
 * from eap_schemas PROCESSOR_DECLARATIONS), and a `GET /api/v1/models` item list. */
import type { ModelOption, ProcessorCatalog } from "./specSections";

export const processorCatalogFixture: ProcessorCatalog = {
  "items": [
    {
      "id": "pii_redact",
      "title": "PII redaction",
      "description": "Detects email addresses, phone numbers, credit card numbers (Luhn-checked), IBANs, IPv4 addresses and US social security numbers; redacts them or blocks the text.",
      "phases": [
        "input",
        "output"
      ],
      "actions": [
        "redact",
        "block"
      ],
      "deterministic": true,
      "config_schema": {
        "additionalProperties": false,
        "properties": {
          "kinds": {
            "default": [
              "email",
              "phone",
              "credit_card",
              "iban",
              "ipv4",
              "us_ssn"
            ],
            "description": "Kinds of personal data to detect",
            "items": {
              "enum": [
                "email",
                "phone",
                "credit_card",
                "iban",
                "ipv4",
                "us_ssn"
              ],
              "type": "string"
            },
            "minItems": 1,
            "type": "array"
          },
          "action": {
            "default": "redact",
            "description": "Redact matches or block the text",
            "enum": [
              "redact",
              "block"
            ],
            "type": "string"
          }
        },
        "type": "object"
      }
    },
    {
      "id": "regex_blocklist",
      "title": "Regex blocklist",
      "description": "Blocks text matching any of up to 20 RE2 patterns (linear time), or redacts the matches.",
      "phases": [
        "input",
        "output"
      ],
      "actions": [
        "block",
        "redact"
      ],
      "deterministic": true,
      "config_schema": {
        "additionalProperties": false,
        "properties": {
          "patterns": {
            "description": "RE2 patterns, at most 20 of 200 characters",
            "items": {
              "maxLength": 200,
              "minLength": 1,
              "type": "string"
            },
            "maxItems": 20,
            "minItems": 1,
            "type": "array"
          },
          "action": {
            "default": "block",
            "description": "Block the text or redact matches",
            "enum": [
              "block",
              "redact"
            ],
            "type": "string"
          }
        },
        "required": [
          "patterns"
        ],
        "type": "object"
      }
    },
    {
      "id": "max_length",
      "title": "Maximum length",
      "description": "Blocks or truncates text longer than a number of characters.",
      "phases": [
        "input",
        "output"
      ],
      "actions": [
        "block",
        "truncate"
      ],
      "deterministic": true,
      "config_schema": {
        "additionalProperties": false,
        "properties": {
          "max_chars": {
            "description": "Maximum length in characters",
            "maximum": 1000000,
            "minimum": 1,
            "type": "integer"
          },
          "action": {
            "default": "block",
            "description": "Block or truncate longer text",
            "enum": [
              "block",
              "truncate"
            ],
            "type": "string"
          }
        },
        "required": [
          "max_chars"
        ],
        "type": "object"
      }
    },
    {
      "id": "moderation",
      "title": "Moderation",
      "description": "Asks an allowlisted model for a verdict over a fixed category set and blocks flagged text. On output it checks the final answer once.",
      "phases": [
        "input",
        "output"
      ],
      "actions": [
        "block"
      ],
      "deterministic": false,
      "config_schema": {
        "additionalProperties": false,
        "properties": {
          "model": {
            "additionalProperties": false,
            "description": "Model that answers the moderation check (must be allowlisted)",
            "properties": {
              "provider": {
                "type": "string"
              },
              "name": {
                "type": "string"
              }
            },
            "required": [
              "provider",
              "name"
            ],
            "type": "object"
          },
          "categories": {
            "default": [
              "hate",
              "harassment",
              "self_harm",
              "sexual",
              "sexual_minors",
              "violence",
              "illicit"
            ],
            "description": "Categories that block the text",
            "items": {
              "enum": [
                "hate",
                "harassment",
                "self_harm",
                "sexual",
                "sexual_minors",
                "violence",
                "illicit"
              ],
              "type": "string"
            },
            "minItems": 1,
            "type": "array"
          },
          "action": {
            "default": "block",
            "description": "Moderation can only block",
            "type": "string",
            "enum": [
              "block"
            ]
          }
        },
        "required": [
          "model"
        ],
        "type": "object"
      }
    }
  ],
  "limits": {
    "max_processors_per_phase": 8,
    "max_regex_patterns": 20,
    "max_regex_pattern_length": 200,
    "max_test_sample_bytes": 32768
  },
  "moderation_categories": [
    "hate",
    "harassment",
    "self_harm",
    "sexual",
    "sexual_minors",
    "violence",
    "illicit"
  ]
};

export const modelsFixture: ModelOption[] = [
  {
    "id": "test:echo",
    "provider": "test",
    "name": "echo",
    "wildcard": false,
    "source": "allowlist",
    "description": null
  },
  {
    "id": "litellm:*",
    "provider": "litellm",
    "name": "*",
    "wildcard": true,
    "source": "allowlist",
    "description": null
  },
  {
    "id": "test:moderation",
    "provider": "test",
    "name": "moderation",
    "wildcard": false,
    "source": "scripted",
    "description": "Moderation verdicts: flags text containing FLAG_ME"
  }
];
