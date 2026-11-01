{
  lib,
  python3Packages,
  qt6,
  makeWrapper,
  ffmpeg,
  gpu-screen-recorder,
  wf-recorder,
  wl-clipboard,
  xclip,
  xdotool,
  xprop,
  wmctrl,
  cloudflared,
  kdotool,
  xdg-utils,
  withQtWebEngine ? true,
  extraRuntimePaths ? [ ],
}:

let
  runtimeDeps = [
    ffmpeg
    gpu-screen-recorder
    wf-recorder
    wl-clipboard
    xclip
    xdotool
    xprop
    wmctrl
    cloudflared
    kdotool
    # replaces the service's PATH, so without this NixOS has no xdg-open (#283).
    xdg-utils
  ] ++ extraRuntimePaths;
in
python3Packages.buildPythonApplication rec {
  pname = "vice-clipper";
  version = builtins.head (builtins.match ''.*__version__ = "([^"]+)".*'' (builtins.readFile ../vice/__init__.py));
  pyproject = true;

  src = lib.cleanSource ../.;

  build-system = with python3Packages; [ setuptools wheel ];

  nativeBuildInputs = [ makeWrapper ] ++ lib.optional withQtWebEngine qt6.wrapQtAppsHook;

  buildInputs = lib.optionals withQtWebEngine [ qt6.qtbase qt6.qtwebengine ];

  dependencies = with python3Packages; [
    evdev
    aiohttp
    click
    tomli-w
    psutil
    pywebview
  ] ++ lib.optionals withQtWebEngine [ pyqt6 pyqt6-webengine qtpy ];

  nativeCheckInputs = [ python3Packages.unittestCheckHook ];
  unittestFlagsArray = [ "-s" "tests" ];

  # The em-dash guard walks the whole tree and would scan setuptools' build/ copy of the bundle.
  preCheck = ''
    export HOME=$(mktemp -d)
    rm -rf build
  '';

  dontWrapQtApps = true;

  postInstall = ''
    install -Dm644 vice.desktop $out/share/applications/vice.desktop
    install -Dm644 assets/vice.svg $out/share/icons/hicolor/scalable/apps/vice.svg
    install -Dm644 packaging/vice.rules $out/lib/udev/rules.d/70-vice-input.rules

    install -Dm644 packaging/vice.service $out/lib/systemd/user/vice.service
    substituteInPlace $out/lib/systemd/user/vice.service \
      --replace-fail /usr/bin/vice $out/bin/vice
  '';

  preFixup = lib.optionalString withQtWebEngine ''
    makeWrapperArgs+=("''${qtWrapperArgs[@]}")
  '';

  postFixup = ''
    for bin in $out/bin/vice $out/bin/vice-app; do
      wrapProgram $bin --prefix PATH : ${lib.makeBinPath runtimeDeps}
    done
  '';

  meta = {
    description = "Instant-replay game clip recorder for Linux, for Wayland and X11";
    homepage = "https://github.com/splitshotz/Vice";
    license = lib.licenses.gpl3Plus;
    mainProgram = "vice-app";
    platforms = lib.platforms.linux;
  };
}
