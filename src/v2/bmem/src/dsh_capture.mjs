import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';

export const name = 'bmem-v2-capture';

function text(content) {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content.map(block => {
    if (['text', 'input_text', 'output_text'].includes(block?.type)) return block.text || '';
    if (['tool_result', 'tool-output', 'tool_output'].includes(block?.type)) return text(block.content ?? block.output);
    return '';
  }).filter(Boolean).join('\n');
}

export function apply(ctx, config) {
  ctx.on('session/event', (session, event) => {
    const sessionId = session?.id || session?.header?.id;
    if (!sessionId || !event) return;
    const user = event.type === 'user/message';
    const assistant = event.type === 'assistant/message';
    const tool = event.type === 'tool/result';
    if (!user && !assistant && !tool) return;
    const message = user ? event.data : event.data?.message;
    const source = message?.source?.kind;
    if (source && (user ? !['user','user-rpc'].includes(source) : source !== (tool ? 'tool' : 'model'))) return;
    const content = text(message?.content).trim().slice(0, 100000);
    if (!content || content.includes('[bmem-injection:')) return;
    const identity = String(message?.id ?? event.seq ?? createHash('sha256').update(content).digest('hex'));
    const request = {
      session_id: String(sessionId), prompt_id: identity,
      hook_event_name: user ? 'UserPromptSubmit' : tool ? 'PostToolUse' : 'AssistantMessage',
      prompt: content, tool_use_id: identity, tool_name: 'dsh-tool', tool_response: content,
    };
    const child = spawn(config.command, config.args, { stdio: ['pipe', 'ignore', 'ignore'], timeout: 12000 });
    child.on('error', () => {});
    child.stdin.on('error', () => {});
    child.stdin.end(JSON.stringify(request));
  });
}
