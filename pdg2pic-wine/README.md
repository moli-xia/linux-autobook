# Pdg2PicAuto 04H fallback

This directory packages the native 32-bit `Pdg2Pic.exe` program in a Wine
container for the central gateway. It is not a resident service. The gateway
creates one networkless, resource-limited container when a Worker has already
identified an `HH` PDG page whose type byte is exactly `0x04`; the container is
removed after the PDF response is sent.

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

Do not broaden `PDG2PIC_FALLBACK_HH_TYPES` into an inverse support list. Parse
errors, damaged files, and unknown markers must continue to fail normally.
