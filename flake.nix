{
  description = "OpenWRT UCI helper for 802.11r/k/v roaming configuration";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python3.withPackages (ps: [ ps.pyyaml ]);
      in
      {
        devShells.default = pkgs.mkShell {
          packages = [
            python
            pkgs.openssh
            pkgs.sshpass
          ];
        };
      }
    );
}
