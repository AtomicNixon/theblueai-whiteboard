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

export interface SnapshotOut {
  canvas: CanvasOut
  elements: ElementOut[]
  me: string
}

// WebSocket op shapes broadcast by the server.
export type WsOp =
  | { op: 'snapshot'; canvas: CanvasOut; elements: ElementOut[]; me: string }
  | { op: 'add'; element: ElementOut }
  | { op: 'update'; element: ElementOut }
  | { op: 'delete'; element_id: string }
  | { op: 'error'; message: string }
