{
  lib,
  python3Packages,
  claude-code,
  gitFull,
}:

python3Packages.buildPythonApplication {
  pname = "taylmail";
  version = "0.1.0";
  pyproject = true;

  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./pyproject.toml
      ./src
      ./tests
    ];
  };

  build-system = [ python3Packages.setuptools ];

  dependencies = with python3Packages; [
    claude-agent-sdk
    fastapi
    uvicorn
  ];

  # The agent runs git send-email, which plain git lacks, and taylmail a2u.
  makeWrapperArgs = [
    "--set-default"
    "TAYLMAIL_CLAUDE"
    (lib.getExe claude-code)
    "--prefix"
    "PATH"
    ":"
    "${lib.makeBinPath [ gitFull ]}:${placeholder "out"}/bin"
  ];

  nativeCheckInputs = [ python3Packages.unittestCheckHook ];
  unittestFlags = [ "-s" "tests" ];

  pythonImportsCheck = [ "taylmail" ];

  meta = {
    description = "Talk to a Claude Code agent over email and chat: patch series out, replies in";
    mainProgram = "taylmail";
    platforms = lib.platforms.linux;
  };
}
