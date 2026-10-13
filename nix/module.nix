{ config, lib, pkgs, ... }:

let
  cfg = config.services.vice;
in
{
  options.services.vice = {
    enable = lib.mkEnableOption "the Vice game clip recorder";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.vice-clipper;
      defaultText = lib.literalExpression "pkgs.vice-clipper";
      description = "The Vice package to use.";
    };

    autoStart = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Start the clipping daemon with the graphical session. With this off,
        Vice only records while its window is open.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ cfg.package cloudflared ];

    # Without the setcap wrapper gsr-kms-server goes through pkexec on every recorder restart.
    programs.gpu-screen-recorder.enable = true;

    # TAG+="uaccess" on the event nodes, so the hotkey listener can read the
    # keyboard without the user being in the `input` group.
    services.udev.packages = [ cfg.package ];

    systemd.user.services.vice = {
      description = "Vice game clip recorder daemon";
      after = [ "graphical-session.target" ];
      wantedBy = lib.optionals cfg.autoStart [ "graphical-session.target" "default.target" ];

      unitConfig = {
        StartLimitIntervalSec = 60;
        StartLimitBurst = 3;
      };

      serviceConfig = {
        Type = "simple";
        ExecStart = "${cfg.package}/bin/vice start --no-open-ui";
        Restart = "on-failure";
        RestartSec = 3;
        # Everything the recorder needs to find the session is set by the
        # compositor, not by us, so import it rather than hardcoding a guess.
        # XAUTHORITY is the X cookie; without it X refuses the recorder (#231).
        PassEnvironment = "WAYLAND_DISPLAY DISPLAY XAUTHORITY XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS XDG_SESSION_TYPE XDG_CURRENT_DESKTOP";
      };
    };
  };
}
