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

  // HEAT and AUTO are rejected outright by this hardware (cooling-only
  // unit, confirmed both on and off), and fan speed LOW is likewise
  // rejected - only these values actually stick.
  readonly property var modes: ["COOL", "DRY", "FAN"]
  readonly property var fanSpeeds: ["AUTO", "MID1", "MID2", "HIGH"]

  property var zones: []
  property string lastError: ""
  property var pendingQueue: []

  readonly property var activeZones: zones.filter(function(z) { return z.drive === "ON" })
  readonly property bool anyOn: activeZones.length > 0
  readonly property string avgTempLabel: {
    var withReading = activeZones.filter(function(z) { return typeof z.inletTemp === "number" })
    if (withReading.length === 0) return ""
    var sum = 0
    for (var i = 0; i < withReading.length; i++) sum += withReading[i].inletTemp
    return Math.round(sum / withReading.length) + "°"
  }
  readonly property string icon: "❄"

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
          inletTemp: u.inlet_temp
        })
      }
      next.sort(function(a, b) { return a.group - b.group })
      root.zones = next
      root.lastError = ""
    } catch (e) {
      root.lastError = "Bad status response"
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
    var idx = root.modes.indexOf(zone.mode)
    var next = root.modes[(idx + 1) % root.modes.length]
    enqueue(["set-mode", String(zone.group), next])
  }

  function cycleFan(zone) {
    var idx = root.fanSpeeds.indexOf(zone.fanSpeed)
    var next = root.fanSpeeds[(idx + 1) % root.fanSpeeds.length]
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
  Component.onCompleted: refresh()

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
        if (t !== "") root.lastError = "Can't reach AC gateway"
      }
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
    text: root.anyOn && root.avgTempLabel !== "" ? (root.icon + " " + root.avgTempLabel) : root.icon
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
