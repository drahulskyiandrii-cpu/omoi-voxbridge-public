// VoxBridge-owned OpenVINO worker. Only the installed SDK and model are shared.
import { createRequire } from 'node:module';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createInterface } from 'node:readline';
import { performance } from 'node:perf_hooks';

const MAX_LINE = 1400000;
const MAX_TEXT = 3000;
const emit = packet => process.stdout.write(JSON.stringify(packet) + '\n');

function decodeWav(encoded) {
  if (typeof encoded !== 'string' || encoded.length > MAX_LINE)
    throw Error('INVALID_AUDIO');
  const raw = Buffer.from(encoded, 'base64');
  if (raw.length < 44 || raw.length > 1048576 || raw.toString('base64') !== encoded ||
      raw.toString('ascii', 0, 4) !== 'RIFF' || raw.toString('ascii', 8, 12) !== 'WAVE')
    throw Error('INVALID_WAV');

  let format, data;
  for (let p = 12; p + 8 <= raw.length;) {
    const size = raw.readUInt32LE(p + 4);
    const start = p + 8;
    const end = start + size;
    if (end > raw.length) throw Error('INVALID_WAV');
    const tag = raw.toString('ascii', p, p + 4);
    if (tag === 'fmt ' && size >= 16) format = raw.subarray(start, end);
    if (tag === 'data') data = raw.subarray(start, end);
    p = end + (size & 1);
  }
  if (!format || !data || format.readUInt16LE(0) !== 1 ||
      format.readUInt16LE(2) !== 1 || format.readUInt32LE(4) !== 16000 ||
      format.readUInt16LE(14) !== 16 || data.length % 2 ||
      data.length < 3200 || data.length > 960000)
    throw Error('INVALID_WAV');

  const samples = new Float32Array(data.length / 2);
  for (let i = 0; i < samples.length; i++)
    samples[i] = data.readInt16LE(i * 2) / 32768;
  return samples;
}

function validateRequest(request, enhanced) {
  if (!request || !Number.isSafeInteger(request.id) || request.id < 1 ||
      !['ru', 'de'].includes(request.language)) throw Error('REQUEST');
  if (enhanced) {
    if (typeof request.hotwords !== 'string' || Buffer.byteLength(request.hotwords, 'utf8') > 192 ||
        /[\p{C}]/u.test(request.hotwords) || typeof request.stream !== 'boolean')
      throw Error('REQUEST');
  } else if ('hotwords' in request || 'stream' in request) {
    throw Error('REQUEST');
  }
}

function generationConfig(request, samples, enhanced) {
  const config = {
    task: 'transcribe',
    language: '<|' + request.language + '|>',
    return_timestamps: false,
    // Preserve the original CPU baseline; enhanced is still explicit opt-in.
    max_new_tokens: enhanced
      ? Math.min(384, Math.max(160, Math.ceil(samples.length / 16000 * 16)))
      : 160,
  };
  if (enhanced && request.hotwords) config.hotwords = request.hotwords;
  return config;
}

function streamingOptions(sdk, request, started) {
  let draft = '', count = 0, first = null, last = -1000, overflow = false;
  return {
    streamer(chunk) {
      if (typeof chunk === 'string' && chunk.length) {
        if (draft.length + chunk.length > MAX_TEXT) {
          overflow = true;
          return sdk.StreamingStatus.CANCEL;
        }
        draft += chunk;
        const now = Math.round(performance.now() - started);
        if (first === null && draft.trim()) first = now;
        if (count < 100 && now - last >= 100 && draft.trim()) {
          emit({ id: request.id, event: 'partial', text: draft });
          count++;
          last = now;
        }
      }
      return sdk.StreamingStatus.RUNNING;
    },
    finish() {
      if (overflow) throw Error('TEXT_LIMIT');
      return first;
    },
  };
}

async function decode(sdk, pipeline, request, enhanced) {
  validateRequest(request, enhanced);
  const samples = decodeWav(request.audio);
  const options = { generationConfig: generationConfig(request, samples, enhanced) };
  const started = performance.now();
  const streaming = enhanced && request.stream ? streamingOptions(sdk, request, started) : null;
  if (streaming) options.streamer = streaming.streamer;
  const out = await pipeline.generate(samples, options);
  const first = streaming ? streaming.finish() : null;
  const text = String(out.texts?.[0] ?? '').trim();
  if (text.length > MAX_TEXT || text.includes('\0')) throw Error('TEXT');
  const reply = { id: request.id, ok: true, text, wallMs: Math.round(performance.now() - started) };
  if (enhanced) {
    const score = out.scores?.[0];
    Object.assign(reply, { firstChunkMs: first, score: Number.isFinite(score) ? score : null,
      detectedLanguage: out.languages?.[0] ?? null });
  }
  return reply;
}

async function main() {
  const profile = process.env.VB_OPENVINO_PROFILE;
  const enhanced = profile === 'enhanced';
  let sdk, pipeline;
  try {
    const sdkDir = process.env.VOXBRIDGE_OPENVINO_SDK_DIR;
    const model = process.env.OMOI_MINI_STT_MODEL;
    if (!sdkDir || !model || process.env.OMOI_MINI_STT_DEVICE !== 'CPU' ||
        !['baseline', 'enhanced'].includes(profile)) throw Error('CONFIG');
    // createRequire needs an absolute anchor, not an existing Mini source file.
    const require = createRequire(resolve(sdkDir, 'package.json'));
    sdk = await import(pathToFileURL(require.resolve('openvino-genai-node')).href);
    const started = performance.now();
    pipeline = await sdk.ASRPipeline(model, 'CPU', {});
    emit({ event: 'ready', device: 'CPU', loadMs: Math.round(performance.now() - started) });
  } catch {
    emit({ event: 'fatal', error: 'OPENVINO_START_FAILED' });
    return 2;
  }
  for await (const line of createInterface({ input: process.stdin, crlfDelay: Infinity })) {
    let id = null;
    try {
      if (line.length > MAX_LINE) throw Error('SIZE');
      const request = JSON.parse(line);
      id = Number.isSafeInteger(request?.id) ? request.id : null;
      emit(await decode(sdk, pipeline, request, enhanced));
    } catch {
      emit({ id, ok: false, error: 'OPENVINO_DECODE_FAILED' });
    }
  }
  return 0;
}

process.exitCode = await main();
