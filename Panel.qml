import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Ui
import qs.Commons

Panel {
  id: root
  moduleName: "mhvac.ac"
  ipcTarget: "mhvac.ac"

  readonly property string cliPath: Quickshell.env("HOME") + "/.config/omarchy/plugins/mhvac.ac/ac_cli.py"

  // ac_cli.py prints {"error": "..."} to stderr on failure; pull out just
  // the message instead of showing the raw JSON blob.
  function extractErrorMessage(raw) {
    var t = String(raw || "").trim()
    if (t === "") return ""
    try {
      var parsed = JSON.parse(t)
      if (parsed && typeof parsed.error === "string") return parsed.error
    } catch (e) {
      // Not JSON (or malformed) - fall through to the raw text.
    }
    return t
  }

  // Fallback used only for a zone whose config hasn't been through
  // `ac_cli.py discover-features` yet - each zone normally carries its own
  // modes/fanSpeeds from status (different indoor unit models on the same
  // system can support different values; see README).
  readonly property var modes: ["COOL", "DRY", "FAN"]
  readonly property var fanSpeeds: ["AUTO", "MID1", "MID2", "HIGH"]

  property var zones: []
  property string lastError: ""
  property string actionError: ""
  property bool unreachable: false
  property bool statusLoaded: false
  property bool setupRequired: false
  property var pendingQueue: []
  readonly property bool hvacControlsEnabled: root.statusLoaded
    && !root.unreachable
    && !statusProc.running
    && !actionProc.running
    && root.pendingQueue.length === 0

  readonly property var iconTempModes: ["avg", "min", "max", "off"]
  property string iconTempMode: "avg"

  // Room temperature is a live sensor reading independent of drive state,
  // so this covers every zone, not just the ones currently on.
  readonly property string tempSummaryLabel: {
    if (root.iconTempMode === "off") return ""
    var withReading = zones.filter(function(z) { return typeof z.inletTemp === "number" })
    if (withReading.length === 0) return ""
    var vals = withReading.map(function(z) { return z.inletTemp })
    var result
    if (root.iconTempMode === "min") {
      result = Math.min.apply(null, vals)
    } else if (root.iconTempMode === "max") {
      result = Math.max.apply(null, vals)
    } else {
      var sum = 0
      for (var i = 0; i < vals.length; i++) sum += vals[i]
      result = sum / vals.length
    }
    return Math.round(result) + "°"
  }
  // A real Nerd Font glyph (JetBrainsMono Nerd Font "md-snowflake", U+F0717),
  // not the Unicode snowflake (U+2744) - that one gets rendered by a
  // color-emoji fallback font that ignores the `color` property entirely,
  // which is why the bar icon used to always render blue regardless of
  // active/urgent state.
  readonly property string icon: "󰜗"

  function cycleIconTempMode() {
    var idx = root.iconTempModes.indexOf(root.iconTempMode)
    var next = root.iconTempModes[(idx + 1) % root.iconTempModes.length]
    enqueue(["set-icon-temp", next])
  }

  readonly property color onColor: "#5fbf6f"
  readonly property color coolColor: "#5aa9e6"
  readonly property color heatColor: "#e0724a"
  readonly property color dryColor: "#e0b64a"
  readonly property color fanModeColor: "#5ac8b0"
  readonly property color autoColor: "#a888e0"

  function modeColor(mode) {
    if (mode === "COOL" || mode === "AUTOCOOL") return root.coolColor
    if (mode === "HEAT" || mode === "AUTOHEAT") return root.heatColor
    if (mode === "DRY") return root.dryColor
    if (mode === "FAN") return root.fanModeColor
    if (mode === "AUTO") return root.autoColor
    return root.bar.foreground
  }

  function fanColor(speed) {
    if (speed === "LOW") return "#5fbf6f"
    if (speed === "MID1") return "#a3c95a"
    if (speed === "MID2") return "#e0b64a"
    if (speed === "HIGH") return "#e0724a"
    return root.bar.foreground
  }

  function refresh() {
    if (!statusProc.running) statusProc.running = true
  }

  function refreshConfig() {
    if (!configProc.running) configProc.running = true
  }

  function updateConfig(raw) {
    try {
      var data = JSON.parse(raw)
      if (root.iconTempModes.indexOf(data.icon_temp) !== -1) root.iconTempMode = data.icon_temp
      root.setupRequired = data.setup_required === true
    } catch (e) {
      // Keep the current settings on a bad/empty response.
    }
  }

  function updateZones(raw) {
    try {
      var data = JSON.parse(raw)
      if (!Array.isArray(data)) {
        throw new Error("Expected array of zones")
      }
      var next = []
      for (var i = 0; i < data.length; i++) {
        var u = data[i]
        next.push({
          group: u.group,
          name: String(u.name || ("Zone " + u.group)),
          drive: u.drive,
          mode: u.mode,
          setTemp: u.set_temp,
          fanSpeed: u.fan_speed,
          airDirection: u.air_direction,
          inletTemp: u.inlet_temp,
          filterSign: u.filter_sign,
          errorSign: u.error_sign,
          modes: Array.isArray(u.modes) ? u.modes : root.modes,
          fanSpeeds: Array.isArray(u.fan_speeds) ? u.fan_speeds : root.fanSpeeds,
          airDirections: Array.isArray(u.air_directions) ? u.air_directions : []
        })
      }
      next.sort(function(a, b) { return a.group - b.group })
      root.zones = next
      root.lastError = ""
      root.unreachable = false
      root.statusLoaded = true
    } catch (e) {
      root.lastError = "Bad status response"
      root.unreachable = true
    }
  }

  function enqueue(args) {
    // Replace the array so bindings that depend on its length are notified.
    pendingQueue = pendingQueue.concat([args])
    drainQueue()
  }

  function drainQueue() {
    if (actionProc.running) return
    if (pendingQueue.length === 0) return
    var args = pendingQueue[0]
    pendingQueue = pendingQueue.slice(1)
    actionProc.command = ["python3", root.cliPath].concat(args)
    actionProc.running = true
  }

  function togglePower(zone) {
    enqueue([zone.drive === "ON" ? "off" : "on", String(zone.group)])
  }

  function setMode(zone, mode) {
    enqueue(["set-mode", String(zone.group), mode])
  }

  function cycleMode(zone) {
    var list = Array.isArray(zone.modes) ? zone.modes : root.modes
    if (list.length === 0) return
    var idx = list.indexOf(zone.mode)
    var next = list[(idx + 1) % list.length]
    enqueue(["set-mode", String(zone.group), next])
  }

  function cycleFan(zone) {
    var list = Array.isArray(zone.fanSpeeds) ? zone.fanSpeeds : root.fanSpeeds
    if (list.length === 0) return
    var idx = list.indexOf(zone.fanSpeed)
    var next = list[(idx + 1) % list.length]
    enqueue(["set-fan", String(zone.group), next])
  }

  function cycleAirDirection(zone) {
    var list = zone.airDirections || []
    if (list.length === 0) return
    var idx = list.indexOf(zone.airDirection)
    var next = list[(idx + 1) % list.length]
    enqueue(["set-air", String(zone.group), next])
  }

  function airDirectionLabel(direction) {
    if (direction === "HORIZONTAL") return "AIR ↔"
    if (direction === "VERTICAL") return "AIR ↕"
    if (direction === "SWING") return "AIR ↕↔"
    if (direction === "MID0") return "AIR 0"
    if (direction === "MID1") return "AIR 1"
    if (direction === "MID2") return "AIR 2"
    return "AIR"
  }

  function adjustTemp(zone, delta) {
    var base = (zone.setTemp === null || zone.setTemp === undefined) ? 22 : zone.setTemp
    var next = Math.max(16, Math.min(30, base + delta))
    enqueue(["set-temp", String(zone.group), String(next)])
  }

  function allOn() { enqueue(["all-on"]) }
  function allOff() { enqueue(["all-off"]) }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onOpenedChanged: if (opened) refresh()
  Component.onCompleted: {
    refresh()
    refreshConfig()
  }

  Process {
    id: statusProc
    command: ["python3", root.cliPath, "status"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.updateZones(text)
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var t = String(text || "").trim()
        if (t !== "") {
          root.lastError = "Can't reach AC gateway"
          root.unreachable = true
        }
      }
    }
  }

  Process {
    id: configProc
    command: ["python3", root.cliPath, "config"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.updateConfig(text)
    }
  }

  Process {
    id: actionProc
    stderr: StdioCollector {
      id: actionErrText
      waitForEnd: true
    }
    onExited: function(exitCode) {
      if (exitCode !== 0) {
        var msg = root.extractErrorMessage(actionErrText.text)
        root.actionError = msg !== "" ? msg : "Action failed"
      } else {
        root.actionError = ""
      }
      root.refresh()
      root.refreshConfig()
      root.drainQueue()
    }
  }

  // Keeps the bar icon (temp/on-off summary) live even while the popup is closed.
  Timer {
    interval: 30000
    running: true
    repeat: true
    onTriggered: root.refresh()
  }

  // Faster refresh while the popup is open, so button presses reflect quickly.
  Timer {
    interval: 4000
    running: root.opened
    repeat: true
    onTriggered: root.refresh()
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    // Suppress the temperature while unreachable - it would just be a stale
    // last-known reading presented as current. The color change (urgent/red)
    // is the actual "can't reach the gateway" signal.
    text: !root.unreachable && root.tempSummaryLabel !== "" ? (root.icon + " " + root.tempSummaryLabel) : root.icon
    active: root.unreachable
    tooltipText: root.unreachable ? (root.lastError || "Can't reach AC gateway") : ""
    onPressed: function(b) { root.toggle() }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(300))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()

      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(6)

        Item {
          width: parent.width
          implicitHeight: Math.max(heroTitle.implicitHeight, heroActions.implicitHeight)

          Text {
            textFormat: Text.PlainText
            id: heroTitle
            text: "AC"
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
            font.bold: true
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
          }

          Row {
            id: heroActions
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(4)

            Button {
              text: root.iconTempMode === "off"
                ? "OFF"
                : root.iconTempMode.toUpperCase() + ": " + (root.tempSummaryLabel !== "" ? root.tempSummaryLabel : "—")
              tooltipText: "Bar icon temperature: click to cycle avg / min / max / off"
              fontSize: Style.font.caption
              horizontalPadding: Style.space(6)
              verticalPadding: Style.space(2)
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
              bordered: true
              onClicked: root.cycleIconTempMode()
            }

            Button {
              text: "All On"
              enabled: root.hvacControlsEnabled
              opacity: enabled ? 1.0 : 0.45
              fontSize: Style.font.caption
              horizontalPadding: Style.space(6)
              verticalPadding: Style.space(2)
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
              bordered: true
              onClicked: root.allOn()
            }

            Button {
              text: "All Off"
              enabled: root.hvacControlsEnabled
              opacity: enabled ? 1.0 : 0.45
              fontSize: Style.font.caption
              horizontalPadding: Style.space(6)
              verticalPadding: Style.space(2)
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
              bordered: true
              onClicked: root.allOff()
            }
          }
        }

        Text {
          textFormat: Text.PlainText
          visible: root.actionError !== "" || root.lastError !== ""
          text: root.actionError || root.lastError
          color: root.bar.urgent
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.caption
          width: parent.width
        }

        Column {
          width: parent.width
          spacing: Style.space(2)

          Repeater {
            model: root.zones
            delegate: ZoneRow {
              required property var modelData
              width: parent.width
              zone: modelData
            }
          }

          Button {
            visible: root.setupRequired && root.zones.length === 0
            enabled: !statusProc.running && !actionProc.running && root.pendingQueue.length === 0
            opacity: enabled ? 1.0 : 0.45
            text: "Discover zones"
            tooltipText: "Discover zones and create the AC configuration"
            fontSize: Style.font.caption
            horizontalPadding: Style.space(8)
            verticalPadding: Style.space(4)
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            bordered: true
            onClicked: root.enqueue(["zones-init"])
          }

          Text {
            textFormat: Text.PlainText
            visible: !root.setupRequired && root.zones.length === 0
              && root.lastError === "" && root.actionError === ""
            text: root.statusLoaded ? "No zones configured" : "Loading…"
            color: root.bar.foreground
            opacity: 0.6
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }
    }
  }

  component ZoneRow: Column {
    id: zoneRow
    required property var zone
    spacing: Style.space(2)
    bottomPadding: Style.space(4)

    Item {
      width: parent.width
      implicitHeight: Math.max(powerBtn.implicitHeight, zoneName.implicitHeight)

      Button {
        id: powerBtn
        enabled: root.hvacControlsEnabled
        opacity: enabled ? 1.0 : 0.45
        text: "⏻"
        fontSize: Style.font.caption
        horizontalPadding: Style.space(5)
        verticalPadding: Style.space(2)
        foreground: zoneRow.zone.drive === "ON" ? root.onColor : Color.muted
        fontFamily: root.bar.fontFamily
        bordered: true
        active: zoneRow.zone.drive === "ON"
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        onClicked: root.togglePower(zoneRow.zone)
      }

      Text {
        textFormat: Text.PlainText
        id: zoneName
        text: zoneRow.zone.name
          + (zoneRow.zone.errorSign !== null && zoneRow.zone.errorSign !== undefined
             && zoneRow.zone.errorSign !== "OFF" ? "  [FAULT]" : "")
        color: zoneRow.zone.errorSign !== null && zoneRow.zone.errorSign !== undefined
          && zoneRow.zone.errorSign !== "OFF" ? root.bar.urgent : root.bar.foreground
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.bodySmall
        font.bold: true
        elide: Text.ElideRight
        anchors.left: powerBtn.right
        anchors.leftMargin: Style.space(6)
        anchors.right: filterIndicator.left
        anchors.rightMargin: Style.space(6)
        anchors.verticalCenter: parent.verticalCenter
      }

      Item {
        id: filterIndicator
        visible: zoneRow.zone.filterSign === "ON"
        Accessible.name: "Filter ON"
        Accessible.role: Accessible.StaticText
        width: visible ? filterGlyph.implicitWidth : 0
        height: filterGlyph.implicitHeight
        anchors.right: zoneTemp.left
        anchors.rightMargin: visible ? Style.space(6) : 0
        anchors.verticalCenter: parent.verticalCenter

        Text {
          id: filterGlyph
          textFormat: Text.PlainText
          text: "󰵃"
          color: root.bar.urgent
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.bodySmall
        }

        MouseArea {
          id: filterHover
          anchors.fill: parent
          hoverEnabled: true
          acceptedButtons: Qt.NoButton
        }

        PanelToolTip {
          visible: filterHover.containsMouse
          text: "Filter ON"
          fontFamily: root.bar.fontFamily
        }
      }

      Text {
        textFormat: Text.PlainText
        id: zoneTemp
        text: typeof zoneRow.zone.inletTemp === "number" ? zoneRow.zone.inletTemp.toFixed(1) + "°" : "—"
        color: root.bar.foreground
        opacity: 0.6
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.caption
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
      }
    }

    Item {
      width: parent.width
      implicitHeight: Math.max(modeBtn.implicitHeight, tempStepper.implicitHeight)

      Row {
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        spacing: Style.space(4)

        Button {
          id: modeBtn
          enabled: root.hvacControlsEnabled
          opacity: enabled ? 1.0 : 0.45
          visible: zoneRow.zone.modes && zoneRow.zone.modes.length > 0
          text: zoneRow.zone.mode || "—"
          fontSize: Style.font.caption
          horizontalPadding: Style.space(6)
          verticalPadding: Style.space(2)
          foreground: zoneRow.zone.drive === "ON" ? root.modeColor(zoneRow.zone.mode) : Color.muted
          fontFamily: root.bar.fontFamily
          bordered: true
          onClicked: root.cycleMode(zoneRow.zone)
        }

        Button {
          enabled: root.hvacControlsEnabled
          opacity: enabled ? 1.0 : 0.45
          visible: zoneRow.zone.fanSpeeds && zoneRow.zone.fanSpeeds.length > 0
          text: zoneRow.zone.fanSpeed || "—"
          fontSize: Style.font.caption
          horizontalPadding: Style.space(6)
          verticalPadding: Style.space(2)
          foreground: zoneRow.zone.drive === "ON" ? root.fanColor(zoneRow.zone.fanSpeed) : Color.muted
          fontFamily: root.bar.fontFamily
          bordered: true
          onClicked: root.cycleFan(zoneRow.zone)
        }

        Button {
          enabled: root.hvacControlsEnabled
          opacity: enabled ? 1.0 : 0.45
          visible: zoneRow.zone.airDirections && zoneRow.zone.airDirections.length > 0
          text: root.airDirectionLabel(zoneRow.zone.airDirection)
          tooltipText: "Air direction: " + (zoneRow.zone.airDirection || "unknown")
          fontSize: Style.font.caption
          horizontalPadding: Style.space(6)
          verticalPadding: Style.space(2)
          foreground: zoneRow.zone.drive === "ON" ? root.bar.foreground : Color.muted
          fontFamily: root.bar.fontFamily
          bordered: true
          onClicked: root.cycleAirDirection(zoneRow.zone)
        }
      }

      Row {
        id: tempStepper
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        spacing: Style.space(2)

        Button {
          text: "−"
          enabled: root.hvacControlsEnabled
          opacity: enabled ? 1.0 : 0.45
          fontSize: Style.font.caption
          horizontalPadding: Style.space(6)
          verticalPadding: Style.space(2)
          foreground: root.bar.foreground
          fontFamily: root.bar.fontFamily
          bordered: true
          onClicked: root.adjustTemp(zoneRow.zone, -1)
        }

        Text {
          textFormat: Text.PlainText
          text: (zoneRow.zone.setTemp === null || zoneRow.zone.setTemp === undefined ? "—" : zoneRow.zone.setTemp) + "°"
          color: root.bar.foreground
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: true
          width: Style.space(26)
          horizontalAlignment: Text.AlignHCenter
          anchors.verticalCenter: parent.verticalCenter
        }

        Button {
          text: "+"
          enabled: root.hvacControlsEnabled
          opacity: enabled ? 1.0 : 0.45
          fontSize: Style.font.caption
          horizontalPadding: Style.space(6)
          verticalPadding: Style.space(2)
          foreground: root.bar.foreground
          fontFamily: root.bar.fontFamily
          bordered: true
          onClicked: root.adjustTemp(zoneRow.zone, 1)
        }
      }
    }
  }
}
