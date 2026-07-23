# My Unitree

A toolkit for Unitree G1 control, hardware diagnostics, and speech-to-speech
OpenAI Realtime conversation in European Portuguese (`pt-PT`). The conversation
runs with standard computer audio devices or directly on a G1 EDU+ PC2, using
the robot's microphone array and speaker.

The conversation is continuous, uses semantic voice activity detection, and
supports interruption while the assistant is speaking. Responses are short and
conversational by default, with more detail only when requested. It can also
list and execute the official whitelisted G1 arm behaviors through Realtime
function tools.

## Requirements

- Python 3.8 or newer
- An OpenAI API key
- Internet access
- A microphone and speaker, or a Unitree G1 EDU+

OpenAI API usage is billed separately from a ChatGPT subscription.

## Local installation

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
cp .env.example .env
```

Add your API key to `.env`, leave `AUDIO_BACKEND=local`, and run:

```bash
conversation
```

### Voice selection

List the voices supported by the Realtime API:

```bash
conversation --list-voices
```

Choose a voice for one conversation:

```bash
conversation --voice cedar
conversation --voice marin
```

The command-line option overrides `OPENAI_REALTIME_VOICE` from `.env` without
changing the file. Supported voices are `alloy`, `ash`, `ballad`, `coral`,
`echo`, `sage`, `shimmer`, `verse`, `marin`, and `cedar`. OpenAI recommends
`marin` and `cedar` for the best quality. A voice is fixed once the session
starts producing audio; choose another voice by starting a new conversation.

## Unitree installation

On the G1 PC2, install the build tools:

```bash
sudo apt update
sudo apt install -y git cmake build-essential python3-dev python3-venv v4l-utils
```

Then run the included installer from the repository root:

```bash
./scripts/install-unitree.sh
cp .env.example .env
```

The installer creates `.venv`, builds CycloneDDS 0.10.2, downloads the official
Unitree SDK, and installs the application. Everything is kept inside this
project under `.venv` and `.deps`.

The Unitree SDK is installed in editable mode because its regular wheel may
omit namespace packages such as `b2`.

Use these values in `.env`:

```dotenv
OPENAI_API_KEY=replace_with_your_api_key
OPENAI_REALTIME_MODEL=gpt-realtime-2.1
OPENAI_REALTIME_VOICE=marin

AUDIO_BACKEND=unitree
AUDIO_DEBUG=false
ROBOT_BEHAVIOR_TOOLS_ENABLED=true
COMPANY_KNOWLEDGE_TOOLS_ENABLED=true

UNITREE_INTERFACE_IP=192.168.123.164
UNITREE_NETWORK_INTERFACE=eth0
UNITREE_MIC_MULTICAST_GROUP=239.168.123.161
UNITREE_MIC_PORT=5555
```

`UNITREE_NETWORK_INTERFACE` must be the interface carrying the `.164` address:

```bash
ip -br address
```

Enable **Voice Assistant → Wake-up Conversation Mode** in the Unitree app,
then start the client:

```bash
source .venv/bin/activate
conversation
```

The project commands select the project-local CycloneDDS automatically. The
optional `source scripts/activate-unitree.sh` helper is only needed when
running Unitree SDK examples directly.

The microphone array sends PCM16 at 16 kHz. The client converts it to 24 kHz
for OpenAI and converts the response back to 16 kHz for the G1 speaker.

### Behaviors in the conversation

Behavior tools are enabled by default. For example, ask:

```text
Que comportamentos consegues fazer?
Dá-me um abraço.
Faz um high five.
```

Listing behaviors only reads the capabilities reported by the robot. An
explicit voice request such as “dá-me um abraço” executes the corresponding
action immediately, without an additional confirmation turn. Physical tool
calls are serialized and restricted to the same official behavior whitelist
used by `robot behavior run`.

During the conversation, the assistant treats the G1 as its own physical body:
it speaks about gestures in the first person and does not describe the robot as
a separate agent or narrate internal tool execution.

The G1 must be in a compatible arm-action mode. Prepare it first if necessary:

```bash
robot mode set prepared
conversation
```

Tool failures, including an incompatible FSM or a behavior rejected by the
firmware, are returned to the conversation so the assistant can explain the
actual error. Disable all behavior tools when required:

```dotenv
ROBOT_BEHAVIOR_TOOLS_ENABLED=false
```

### DigitalSign knowledge

The conversation has a local `search_digitalsign_knowledge` tool enabled by
default. Whenever someone asks about DigitalSign, the assistant searches the
curated company knowledge before answering instead of relying on the model's
general training data.

The knowledge is stored in:

```text
conversation/knowledge/digitalsign.json
```

Each entry contains a title, search keywords, the information returned to the
model, and the official source URLs that support it. The file is read for each
tool call, so edits become available to the next conversation turn without
rebuilding the application or restarting the conversation.

After changing company facts, update the top-level `updated` date and keep the
corresponding `source_urls`. Disable this tool when required:

```dotenv
COMPANY_KNOWLEDGE_TOOLS_ENABLED=false
```

## Hardware diagnostics

The `diagnostics` command tests the G1 hardware without connecting to
OpenAI. Enable **Voice Assistant → Wake-up Conversation Mode** before testing
the microphone. Run the commands from the repository or set
`UNITREE_PROJECT_ROOT` when running them elsewhere.

By default, all generated files are saved under `diagnostics/result/`. Record
five seconds from the microphone array:

```bash
diagnostics microphone --duration 5
```

Capture a photo from the camera:

```bash
diagnostics photo
```

Capture every image stream exposed by the RealSense:

```bash
diagnostics cameras
```

This creates:

```text
diagnostics/result/camera-color.jpg
diagnostics/result/camera-depth-raw.png
diagnostics/result/camera-depth.jpg
diagnostics/result/camera-infrared-left.png
diagnostics/result/camera-infrared-right.png
```

`camera-depth-raw.png` preserves the 16-bit depth values. `camera-depth.jpg` is
a colour visualization intended for quick inspection.

Record ten seconds of video:

```bash
diagnostics video --duration 10
```

Test the speakers by playing the microphone recording:

```bash
diagnostics speakers
```

The speakers accept another uncompressed mono PCM16 WAV file at 16 kHz:

```bash
diagnostics speakers --input recordings/message.wav
```

Read a safe, read-only snapshot of the robot:

```bash
diagnostics state
```

This reports the current FSM ID and its known meaning, speaker volume, control
modes, DDS tick, IMU, battery/BMS, and every motor reporting activity. The same
data is saved as structured JSON in `diagnostics/result/robot-state.json`. Battery
fields whose units are not documented by the SDK are explicitly labelled
`raw`.

Set the speaker volume from 0 to 100:

```bash
diagnostics volume 65
```

The robot plays a short beep when the level changes. When setting volume to
zero, it beeps immediately before muting so the confirmation remains audible.

## Robot control

The `robot` command contains operations that inspect or change the G1 control
state. These are not hardware diagnostics.

Ask the robot which official arm behaviors it implements:

```bash
robot behavior list
```

Inspect the current operating mode and its description:

```bash
robot mode get
```

List every FSM mode known from the official locomotion and arm-action APIs,
including its description and whether it can be requested directly or is
managed internally by the firmware:

```bash
robot mode list
```

Set a mode by name or FSM ID. Before executing arm behaviors, place the robot
in the official start state (FSM `500`). Make sure it is upright, stable, and
clear of people:

```bash
robot mode set prepared
# Equivalent:
robot mode set 500
```

Execute one behavior by name:

```bash
robot behavior run hug
robot behavior run high five
robot behavior run two-hand kiss
```

The command accepts spaces, hyphens, and underscores interchangeably. It shows
a `y/N` safety confirmation before moving. Use `--yes` only when a
non-interactive caller has already ensured that the robot is stable and nobody
is within arm reach:

```bash
robot behavior run clap --yes
```

Held poses such as `hug`, `high five`, `heart`, and `shake hand` automatically
execute the official `release arm` action after two seconds. Override that
delay with `--hold`:

```bash
robot behavior run hug --hold 4
```

Arm actions require the robot to be in FSM `500`, `501`, or `801`; FSM `801`
also requires mode `0` or `3`. The behavior command checks this state and asks
you to run `robot mode set prepared` if it is incompatible.

When testing is finished, release the arms and return the robot to the official
sitting state (FSM `3`):

```bash
robot mode set rest
```

`mode set` supports the official directly requestable modes `zero torque` (FSM
`0`), `damp` (`1`), `squat` (`2`), `rest` (`3`), `stand` (`4`), `prepared`
(`500`), `lie to stand` (`702`), and `squat transition` (`706`). FSM `501` and
`801` are listed and described, but rejected by `mode set` because they are
firmware-managed states. These modes can move the robot or remove active
posture support, so every mode change requires a `y/N` safety confirmation.
Use `--yes` to skip it when safety has already been ensured.
`mode set prepared` automatically performs the required
`damp → stand → prepared` sequence when starting from a passive state. Leaving
an arm-behavior FSM first executes the official `release arm` action. Error
codes from the official arm service are translated, including occupied
`rt/armsdk` (`7400`), a held pose (`7401`), an invalid action (`7402`), and an
incompatible FSM (`7404`).

## Camera device troubleshooting

Find the local depth and infrared camera devices on the PC2 with:

```bash
v4l2-ctl --list-devices
```

For the G1 RealSense tested by this project, the local streams are:

```dotenv
UNITREE_DEPTH_CAMERA_DEVICE=/dev/video0
UNITREE_INFRARED_CAMERA_DEVICE=/dev/video2
```

The `/dev/video1`, `/dev/video3`, and `/dev/video5` nodes contain metadata, not
images. Device numbers can change when USB hardware changes. Select different
depth or infrared devices for one command with:

```bash
diagnostics cameras \
  --depth-camera /dev/videoX \
  --infrared-camera /dev/videoY
```

The colour device `/dev/video4` is owned by Unitree's `videohub_pc4` service.
Photo and video therefore use the official SDK `VideoClient`, as in Unitree's
front-camera example, instead of opening the device through Video4Linux. Depth
and infrared are captured with `v4l2-ctl` using the raw `Z16` and `Y8I`
formats exposed by their free local V4L devices. MP4 and AVI output are
supported. `--output` can be used with the microphone, photo, and video
commands to override `diagnostics/result/`.

## Configuration

All available settings are documented in [.env.example](.env.example).

Set `AUDIO_DEBUG=true` to print the main audio stages and PCM levels:

```text
G1 array → application
Application → OpenAI
OpenAI → audio
Application → PlayStream
```

If the PCM peak remains at zero, the input is digital silence. If no microphone
packets arrive, check Wake-up Conversation Mode, the network interface, and the
`.164` address.

## Development

Run the offline tests with:

```bash
python -m unittest discover -s tests -v
```

Project layout:

```text
my_unitree/
  configuration.py    shared project and CycloneDDS configuration
diagnostics/
  cli.py              hardware diagnostics command-line entry point
  result/             generated diagnostic files (ignored by Git)
robot/
  cli.py              robot mode and behavior command-line entry point
  services.py         G1 hardware services, state readers, and controls
conversation/
  cli.py              voice conversation command-line entry point
  knowledge/          curated company knowledge and source URLs
  tools.py            validated Realtime tools and knowledge retrieval
  voice.py            Realtime session and audio transports
scripts/
  install-unitree.sh  G1 dependency installation
tests/
  test_robot.py       offline robot and diagnostics tests
  test_voice.py       offline tests
```
