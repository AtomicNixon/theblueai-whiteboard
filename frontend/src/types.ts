// Types matching the backend models.
export interface CanvasOut {
  id: string
  owner_did: string
  title: string
  status: string
  created_at: string
}

export interface ElementOut {
  id: string
  canvas_id: string
  kind: 'text' | 'mark'
  owner_did: string
  data: Record<string, unknown>
  created_at: string
  updated_at: string
}

export interface BinaryFile {
  id: string
  mimeType: string
  dataURL: string
}

export interface SnapshotOut {
  canvas: CanvasOut
  elements: ElementOut[]
  files?: BinaryFile[]
  me: string
}

// WebSocket op shapes broadcast by the server.
export type WsOp =
  | { op: 'snapshot'; canvas: CanvasOut; elements: ElementOut[]; files?: BinaryFile[]; me: string }
  | { op: 'add'; element: ElementOut }
  | { op: 'add_bulk'; elements: ElementOut[] }
  | { op: 'file'; file: BinaryFile }
  | { op: 'update'; element: ElementOut }
  | { op: 'delete'; element_id: string }
  | { op: 'error'; message: string }
