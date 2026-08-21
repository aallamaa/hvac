import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Ui
import qs.Commons

Panel {
  id: root
  moduleName: "mvac.ac"
  ipcTarget: "mvac.ac"

  readonly property string cliPath: Quickshell.env("HOME") + "/.config/omarchy/plugins/mvac.ac/ac_cli.py"

  // Fallback used only for a zone whose config hasn't been through
  // `ac_cli.py discover-features` yet - each zone normally carries its own
  // modes/fanSpeeds from status (different indoor unit models on the same
  // system can support different values; see README).
  readonly property var modes: ["COOL", "DRY", "FAN"]
  readonly property var fanSpeeds: ["AUTO", "MID1", "MID2", "HIGH"]

  property var zones: []
  property string lastError: ""
  property bool unreachable: false
  property var pendingQueue: []

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
    if (mode === "HEAT") return root.heatColor
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
    } catch (e) {
      // Keep the current mode on a bad/empty response.
    }
  }

  function updateZones(raw) {
    try {
      var data = JSON.parse(raw)
      var next = []
      for (var i = 0; i < data.length; i++) {
        var u = data[i]
        next.push({
          group: u.group,
          name: u.name || ("Zone " + u.group),
          drive: u.drive,
          mode: u.mode,
          setTemp: u.set_temp,
          fanSpeed: u.fan_speed,
          inletTemp: u.inlet_temp,
          modes: (u.modes && u.modes.length) ? u.modes : root.modes,
          fanSpeeds: (u.fan_speeds && u.fan_speeds.length) ? u.fan_speeds : root.fanSpeeds
        })
      }
      next.sort(function(a, b) { return a.group - b.group })
      root.zones = next
      root.lastError = ""
      root.unreachable = false
    } catch (e) {
      root.lastError = "Bad status response"
      root.unreachable = true
    }
  }

  function enqueue(args) {
    pendingQueue.push(args)
    drainQueue()
  }

  function drainQueue() {
    if (actionProc.running) return
    if (pendingQueue.length === 0) return
    actionProc.command = ["/usr/bin/python3", root.cliPath].concat(pendingQueue.shift())
    actionProc.running = true
  }

  function togglePower(zone) {
    enqueue([zone.drive === "ON" ? "off" : "on", String(zone.group)])
  }

  function setMode(zone, mode) {
    enqueue(["set-mode", String(zone.group), mode])
  }

  function cycleMode(zone) {
    var list = (zone.modes && zone.modes.length) ? zone.modes : root.modes
    var idx = list.indexOf(zone.mode)
    var next = list[(idx + 1) % list.length]
    enqueue(["set-mode", String(zone.group), next])
  }

  function cycleFan(zone) {
    var list = (zone.fanSpeeds && zone.fanSpeeds.length) ? zone.fanSpeeds : root.fanSpeeds
    var idx = list.indexOf(zone.fanSpeed)
    var next = list[(idx + 1) % list.length]
    enqueue(["set-fan", String(zone.group), next])
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
    command: ["/usr/bin/python3", root.cliPath, "status"]
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
    command: ["/usr/bin/python3", root.cliPath, "config"]
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
        var t = String(actionErrText.text || "").trim()
        root.lastError = t !== "" ? t : "Action failed"
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
          visible: root.lastError !== ""
          text: root.lastError
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

          Text {
            visible: root.zones.length === 0 && root.lastError === ""
            text: "Loading…"
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
        id: zoneName
        text: zoneRow.zone.name
        color: root.bar.foreground
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.bodySmall
        font.bold: true
        elide: Text.ElideRight
        anchors.left: powerBtn.right
        anchors.leftMargin: Style.space(6)
        anchors.right: zoneTemp.left
        anchors.rightMargin: Style.space(6)
        anchors.verticalCenter: parent.verticalCenter
      }

      Text {
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
          text: zoneRow.zone.fanSpeed || "—"
          fontSize: Style.font.caption
          horizontalPadding: Style.space(6)
          verticalPadding: Style.space(2)
          foreground: zoneRow.zone.drive === "ON" ? root.fanColor(zoneRow.zone.fanSpeed) : Color.muted
          fontFamily: root.bar.fontFamily
          bordered: true
          onClicked: root.cycleFan(zoneRow.zone)
        }
      }

      Row {
        id: tempStepper
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        spacing: Style.space(2)

        Button {
          text: "−"
          fontSize: Style.font.caption
          horizontalPadding: Style.space(6)
          verticalPadding: Style.space(2)
          foreground: root.bar.foreground
          fontFamily: root.bar.fontFamily
          bordered: true
          onClicked: root.adjustTemp(zoneRow.zone, -1)
        }

        Text {
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
