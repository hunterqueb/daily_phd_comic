# Build the container image and publish it to the GitHub Container
# Registry (ghcr.io). Push to main gives the "latest" tag. A tag such as
# v1.2.0 gives the "1.2.0" and "1.2" tags.
name: Publish image

on:
  push:
    branches: [main]
    tags: ["v*.*.*"]
  workflow_dispatch:

permissions:
  contents: read
  packages: write

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: Get the code
        uses: actions/checkout@v5

      # QEMU and Buildx make images for x86 and ARM (Raspberry Pi).
      - name: Set up QEMU
        uses: docker/setup-qemu-action@v3

      - name: Set up Buildx
        uses: docker/setup-buildx-action@v3

      - name: Log in to ghcr.io
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      # This step also makes the image name lowercase. Registries need it.
      - name: Make tags and labels
        id: meta
        uses: docker/metadata-action@v5
        with:
          images: ghcr.io/${{ github.repository }}
          tags: |
            type=raw,value=latest,enable={{is_default_branch}}
            type=semver,pattern={{version}}
            type=semver,pattern={{major}}.{{minor}}
            type=sha,format=short

      - name: Build and push
        uses: docker/build-push-action@v6
        with:
          context: .
          platforms: linux/amd64,linux/arm64
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
