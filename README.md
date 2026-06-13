<div align="center">

```


 █████╗ ██████╗ ██████╗ ██╗   ██╗██╗███████╗
██╔══██╗██╔══██╗██╔══██╗██║   ██║██║██╔════╝
███████║██████╔╝██████╔╝██║   ██║██║███████╗
██╔══██║██╔══██╗██╔══██╗╚██╗ ██╔╝██║╚════██║
██║  ██║██████╔╝██████╔╝ ╚████╔╝ ██║███████║
╚═╝  ╚═╝╚═════╝ ╚═════╝   ╚═══╝  ╚═╝╚══════╝


```

# 🤖 J.A.R.V.I.S — Local AI Desktop Assistant

**Just A Rather Very Intelligent System**

*Your personal AI assistant that listens, thinks, and acts — entirely on your machine.*

[![Python](https://img.shields.io/badge/Python-3.11+-blue?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![Ollama](https://img.shields.io/badge/Ollama-Llama%203.1-olive?style=for-the-badge&logo=ollama&logoColor=white)](https://ollama.ai)
[![Whisper](https://img.shields.io/badge/Whisper-STT-red?style=for-the-badge&logo=openai&logoColor=white)](https://github.com/openai/whisper)
[![ElevenLabs](https://img.shields.io/badge/ElevenLabs-TTS-purple?style=for-the-badge&logo=elevenlabs&logoColor=white)](https://elevenlabs.io)
[![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](LICENSE)

---

## 🎬 See It In Action

<div align="center">

**👆 Double Clap to Wake → Speak Command → Jarvis Executes**

```
  🖐️🤚 CLAP CLAP
       ↓
  🎤 Listening...
       ↓
  🧠 "Open GitHub"
       ↓
  🌐 *opens github.com*
       ↓
  🔊 "Opening GitHub, sir."
```

</div>

---

## ⚡ Features

<table>
<tr>
<td align="center" width="33%">

### 🎤 Voice Control
Double-clap wake word
<br>Whisper STT transcription
<br>Noisy input handling
<br>Filler word filtering

</td>
<td align="center" width="33%">

### 🧠 AI Router
Llama 3.1 via Ollama
<br>Intent classification
<br>JSON action output
<br>Error recovery

</td>
<td align="center" width="33%">

### 🛠️ Actions
Open apps & URLs
<br>Play music on Spotify
<br>Web page reading
<br>GitHub repo info

</td>
</tr>
<tr>
<td align="center" width="33%">

### 🌐 Agent Reach
Jina Reader integration
<br>YouTube summarization
<br>Web content extraction
<br>Zero-config setup

</td>
<td align="center" width="33%">

### 🔊 TTS Voice
ElevenLabs TTS
<br>"Yes, sir?" greeting
<br>Cached audio playback
<br>Multilingual support

</td>
<td align="center" width="33%">

### 📊 Metrics
Command tracking
<br>Success rate logging
<br>Latency monitoring
<br>Session summaries

</td>
</tr>
</table>

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    🖐️ DOUBLE CLAP DETECTOR                  │
│              Audio RMS → Spike Detection → Trigger          │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                    🎤 WHISPER STT                           │
│              Audio Buffer → faster-whisper → Text           │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                    🧠 LLAMA 3.1 ROUTER                      │
│              Text → Intent Classification → JSON            │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                    ⚡ ACTION EXECUTOR                        │
│         ┌──────────┬──────────┬──────────┬──────────┐       │
│         │ Open URL │ Open App │ Read Web │ GitHub   │       │
│         └──────────┴──────────┴──────────┴──────────┘       │
└─────────────────────────────────────────────────────────────┘
```

---

## 🚀 Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/shivamsingh-007/Jarvis-Local-assistant.git
cd Jarvis-Local-assistant
```

### 2. Install Dependencies

```bash
python -m pip install -r requirements.txt
```

### 3. Configure Environment

Create a `.env` file in the project root:

```env
# Required for TTS
ELEVENLABS_API_KEY=your_api_key_here
ELEVENLABS_VOICE_ID=your_voice_id_here

# Optional: Custom Ollama model
OLLAMA_CMD_MODEL=llama3.1:8b

# Optional: VS Code path (if not in PATH)
VSCODE_EXE_PATH=C:\Users\You\AppData\Local\Programs\Microsoft VS Code\Code.exe
```

### 4. Pull the LLM Model

```bash
ollama pull llama3.1:8b
```

### 5. Run Jarvis

```bash
python jarvis.py
```

**Or use CLI mode (no microphone needed):**

```bash
python jarvis.py --cli
```

---

## 🎯 Supported Commands

| Voice Command | Action | Example |
|--------------|--------|---------|
| `"Open Perplexity"` | 🌐 Open URL | Opens perplexity.ai |
| `"Open GitHub"` | 🌐 Open URL | Opens github.com |
| `"Open Notion"` | 🌐 Open URL | Opens notion.so |
| `"Open VS Code"` | 💻 Open App | Launches VS Code |
| `"Open Spotify"` | 🎵 Open App | Launches Spotify |
| `"Play lofi beats"` | 🎵 Play Music | Searches Spotify |
| `"Read this page"` | 📖 Read Web | Extracts page content |
| `"What is RAG?"` | 💬 System Message | AI answers |

---

## 🧪 Testing

Run the full test suite:

```bash
python jarvis.py --test
```

Expected output:

```
  [PASS] "open perplexity" => open_url
  [PASS] "open vs code" => open_app
  [PASS] "play lofi on spotify" => open_app
  [PASS] "what is RAG?" => system_message
  [PASS] "read this page https://example.com" => read_web
  [PASS] "what is the pipecat repo" => github_info
  ...
  26 passed, 0 failed, 26 total
```

---

## ⚙️ Configuration

### Audio Tuning

| Parameter | Default | Description |
|-----------|---------|-------------|
| `SPIKE_RATIO` | `4.5` | Clap detection sensitivity |
| `COOLDOWN_S` | `1.0` | Min seconds between claps |
| `BLOCK_MS` | `50` | Audio block size (ms) |
| `MIN_RMS` | `0.01` | Minimum audio threshold |
| `SAMPLE_RATE` | `44100` | Audio sample rate |

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ELEVENLABS_API_KEY` | ✅ | ElevenLabs API key |
| `ELEVENLABS_VOICE_ID` | ✅ | ElevenLabs voice ID |
| `OLLAMA_CMD_MODEL` | ❌ | LLM model (default: `llama3.1:8b`) |
| `OLLAMA_CMD_URL` | ❌ | Ollama endpoint |
| `VSCODE_EXE_PATH` | ❌ | Custom VS Code path |
| `JARVIS_INPUT_DEVICE` | ❌ | Audio input device ID |

---

## 📁 Project Structure

```
Jarvis-Local-assistant/
├── jarvis.py          # Main application (all-in-one)
├── requirements.txt   # Python dependencies
├── .env              # Environment config (not in git)
├── .gitignore        # Git ignore rules
└── README.md         # This file
```

---

## 🛡️ Security Notes

- 🔐 `.env` file is **never** committed (contains API keys)
- 🎤 Audio is processed **locally** (Whisper runs on your machine)
- 🧠 LLM runs **locally** via Ollama (no data sent to cloud)
- 🌐 Only URLs you explicitly configure are opened
- ⚠️ No arbitrary command execution (whitelisted actions only)

---

## 🔧 Troubleshooting

| Issue | Solution |
|-------|----------|
| **No reaction to claps** | Lower `SPIKE_RATIO` or clap closer to mic |
| **False triggers** | Raise `SPIKE_RATIO` or `COOLDOWN_S` |
| **No TTS voice** | Check `ELEVENLABS_API_KEY` in `.env` |
| **Ollama errors** | Run `ollama pull llama3.1:8b` |
| **Audio errors** | Try different `SAMPLE_RATE` (44100/48000) |
| **PortAudio issues** | Update audio drivers |

---

## 🗺️ Roadmap

- [x] Double-clap wake detection
- [x] Whisper STT integration
- [x] Llama 3.1 command routing
- [x] App/URL opening
- [x] Spotify music control
- [x] ElevenLabs TTS
- [x] Agent Reach (web reading)
- [x] GitHub repo info
- [ ] Supertonic local TTS
- [ ] Pipecat voice pipelines
- [ ] OpenHuman memory integration
- [ ] Window management
- [ ] Calendar integration
- [ ] Email control

---

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

---

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

## 🙏 Acknowledgments

- **[Ollama](https://ollama.ai)** — Local LLM hosting
- **[Whisper](https://github.com/openai/whisper)** — Speech-to-text
- **[ElevenLabs](https://elevenlabs.io)** — Text-to-speech
- **[Agent Reach](https://github.com/Panniantong/Agent-Reach)** — Web access tools
- **[Jina Reader](https://jina.ai)** — Web content extraction

---

<div align="center">

**Built with ❤️ by [Shivam Singh](https://github.com/shivamsingh-007)**

*"Just a rather very intelligent system"*

---

<img src="https://img.shields.io/badge/MADE%20WITH%20PYTHON-3670A0?style=for-the-badge&logo=python&logoColor=white" alt="Made with Python">

</div>
