# Architecture

The package has a folder per layer. An arrow points from a layer to a layer it imports. The architecture tests in `tests/update_time/test_architecture.py` let a layer import only the layers ranked before it. Run `just architecture` to regenerate this document.

```mermaid
flowchart TD
  domain --> formats
  domain --> primitives
  io --> domain
  io --> markers
  io --> primitives
  manifests --> domain
  manifests --> formats
  manifests --> markers
  manifests --> primitives
  markers --> domain
  markers --> primitives
  package_managers --> domain
  package_managers --> formats
  package_managers --> io
  package_managers --> manifests
  package_managers --> markers
  package_managers --> primitives
  package_managers --> sources
  references --> domain
  references --> formats
  references --> io
  references --> markers
  references --> package_managers
  references --> primitives
  references --> sources
  sources --> domain
  sources --> formats
  sources --> io
  sources --> manifests
  sources --> primitives
  updaters --> domain
  updaters --> formats
  updaters --> io
  updaters --> manifests
  updaters --> markers
  updaters --> package_managers
  updaters --> primitives
  updaters --> references
  updaters --> sources
```
