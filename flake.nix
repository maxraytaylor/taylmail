{
  description = "Talk to a Claude Code agent over email and chat";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      forAllSystems = nixpkgs.lib.genAttrs [
        "x86_64-linux"
        "aarch64-linux"
      ];
      pkgsFor =
        system:
        import nixpkgs {
          inherit system;
          config.allowUnfreePredicate = pkg: nixpkgs.lib.getName pkg == "claude-code";
        };
    in
    {
      overlays.default = final: prev: { taylmail = final.callPackage ./package.nix { }; };

      packages = forAllSystems (system: {
        default = (pkgsFor system).callPackage ./package.nix { };
      });

      checks = forAllSystems (system: {
        default = self.packages.${system}.default;
      });
    };
}
