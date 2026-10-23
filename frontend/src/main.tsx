/**
 * 前端入口。
 *
 * 注意：这里刻意不使用 <React.StrictMode>。
 * StrictMode 会在开发环境重复挂载组件，导致 PixiJS 的 WebGL 上下文被创建两次，
 * 在部分集成显卡驱动上会出现画布黑屏。生产构建不受影响。
 */

import ReactDOM from 'react-dom/client';

import App from './App';
import './styles/global.css';

const container = document.getElementById('root');
if (!container) {
  throw new Error('找不到 #root 容器');
}

ReactDOM.createRoot(container).render(<App />);
