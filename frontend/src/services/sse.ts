export interface ServerEvent {
  event: string;
  data: string;
}

export class SSEParser {
  private buffer = '';
  push(text: string): ServerEvent[] {
    this.buffer += text;
    this.buffer = this.buffer.replace(/\r\n/g, '\n');
    const events: ServerEvent[] = [];
    let end: number;
    while ((end = this.buffer.indexOf('\n\n')) !== -1) {
      const block = this.buffer.slice(0, end);
      this.buffer = this.buffer.slice(end + 2);
      let event = 'message';
      const data: string[] = [];
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim();
        if (line.startsWith('data:')) data.push(line.slice(5).trimStart());
      }
      if (data.length) events.push({ event, data: data.join('\n') });
    }
    return events;
  }
}
