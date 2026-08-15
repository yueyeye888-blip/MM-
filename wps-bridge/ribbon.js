function OnAddinLoad(ribbonUI) { return true; }
function APRBridgeReconcile() {
  if (window.APRReadOnlyBridge) window.APRReadOnlyBridge.reconcile();
  return true;
}
