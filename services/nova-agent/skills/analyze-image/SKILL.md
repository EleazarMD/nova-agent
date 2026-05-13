---
name: analyze-image
tool_name: analyze_image
description: >
  Extract detailed semantic information from any image URL or vision pipeline snapshot.
parameters:
  type: object
  properties:
    url:
      type: string
      description: "Direct URL to image or snapshot reference"
    instruction:
      type: string
      description: "What to look for in the image"
  required:
    - url
    - instruction
---

# Analyze Image
Extract details using vision models.
