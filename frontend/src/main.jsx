import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";

// Mount root React application into #root DOM container.
ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
