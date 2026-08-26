# Pdg2PicAuto on-demand PDG fallback

This directory packages the native 32-bit `Pdg2Pic.exe` program in a Wine
container for the central gateway. It is not a resident service. A confirmed
`HH 04H` page is sent here immediately; every other PDG book is sent only after
the open decoder fails, produces an invalid PDF, or returns an incomplete page
count. The gateway creates one networkless, resource-limited container for the
request and removes it after the PDF response is sent.

The proprietary application is intentionally not copied into this repository.
For a build, stage the existing `Pdg2PicAuto/Pdg2Pic` directory at
`pdg2pic-wine/app/Pdg2PicAuto/Pdg2Pic`, then run:

```sh
docker build --platform linux/386 -t autobook-pdg2pic-wine:local pdg2pic-wine
```

The gateway Docker deployment also needs `/var/run/docker.sock` mounted and
the following gateway settings:

```dotenv
PDG_FALLBACK_ENABLED=1
PDG_FALLBACK_IMAGE=autobook-pdg2pic-wine:local
PDG_FALLBACK_DOCKER_SOCKET=/var/run/docker.sock
PDG_FALLBACK_RUNTIME_VOLUME=autobook-docker_autobook-runtime
```

The endpoint accepts only path-safe ZIP uploads that contain PDG pages. It
validates that the result is a readable, non-empty PDF with exactly one output
page per input PDG page. Search, download, archive extraction, and upload errors
remain outside this fallback because Pdg2Pic cannot repair those stages.
