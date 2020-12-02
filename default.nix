{ pkgs ? import (fetchTarball "https://github.com/NixOS/nixpkgs/archive/8eb28a1d2bc4358188845cb8a788f6d465aa55d6.tar.gz") {} }:
let
  tracedump = pkgs.callPackage (pkgs.fetchzip {
    url = "https://github.com/Mic92/tracedump/archive/cf4ec3fd5b9ccbd347054cefdcfd955f5056c44a.tar.gz";
    sha256 = "sha256-k/QeYUSBOGzmMRshq+9SJOmPDOfIPEb3va+p76252JU=";
  });
in
(pkgs.buildFHSUserEnv {
  name = "parsec";
  targetPkgs = pkgs: (with pkgs; [
    getopt
    flex
    binutils
    bison
    gcc
    gnumake
    bc
    pkg-config
    m4
    which
    cmake
    gettext
    autoconf
    xorg.libX11
    xorg.libX11
    xlibs.xorgproto
    xlibs.libXext
    xlibs.libXt
    xlibs.libXmu
    xlibs.libXi
    xlibs.libXau
    yasm
    # as noted in the README, x264 crashes due to double free.
    # However one can use the upstream version
    x264
    jq
    (python3.withPackages (py: [
      (tracedump { inherit pkgs; inherit (py) python; })
    ]))
  ]);
  # for ocean_ncp this seems to be not defined correctly
  profile = pkgs.lib.optionalString (pkgs.stdenv.is64bit) ''
    export NIX_CFLAGS_COMPILE="$NIX_CFLAGS_COMPILE -DPTRDIFF_MAX=0x7fffffffffffffff"
  '';

  extraOutputsToInstall = [ "dev" ];
  runScript = "bash";
}).env
