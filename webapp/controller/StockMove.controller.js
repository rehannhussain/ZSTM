sap.ui.define([
	"sap/ui/core/mvc/Controller",
	"sap/ui/model/json/JSONModel",
	"sap/ui/core/library",
	"sap/m/MessageToast",
	"sap/m/MessageBox",
	"sap/m/Dialog",
	"sap/m/Button",
	"sap/ui/core/HTML"
], function (Controller, JSONModel, coreLibrary, MessageToast, MessageBox, Dialog, Button, HTML) {
	"use strict";

	var ValueState = coreLibrary.ValueState;

	return Controller.extend("stock.transfer.controller.StockMove", {

		onInit: function () {
			this.getView().setModel(this._newModel(), "form");
			this._checkHealth();
			this._healthTimer = setInterval(this._checkHealth.bind(this), 20000);
		},

		onExit: function () {
			this._stopCamera();
			if (this._healthTimer) { clearInterval(this._healthTimer); }
		},

		/** Fresh state. Destination + operator are kept across postings on purpose
		 *  so an operator can build several lists to the same location in a row. */
		_newModel: function () {
			return new JSONModel({
				sapOnline: true,
				mock: false,
				toSloc: "3055",          // fixed destination storage location
				operator: "",
				scanText: "",
				busy: false,
				cart: [],
				hasResult: false,
				result: {},
				history: []
			});
		},

		// ----- SAP availability ---------------------------------------------

		_checkHealth: function () {
			var oModel = this.getView().getModel("form");
			fetch("api/health")
				.then(function (res) { return res.json(); })
				.then(function (body) {
					var bOnline = !!(body && body.ok);
					var bWas = oModel.getProperty("/sapOnline");
					oModel.setProperty("/sapOnline", bOnline);
					oModel.setProperty("/mock", !!(body && body.mock));
					if (bOnline && bWas === false) {
						MessageToast.show(this._t("sapBackOnline"));
					}
				}.bind(this))
				.catch(function () {
					oModel.setProperty("/sapOnline", false);
				});
		},

		onRetryHealth: function () {
			this._checkHealth();
		},

		onDestChange: function (oEvent) {
			// Normalise the destination to upper-case as it is typed.
			var oInput = oEvent.getSource();
			var sVal = (oInput.getValue() || "").toUpperCase();
			if (sVal !== oInput.getValue()) {
				oInput.setValue(sVal);
			}
			oInput.setValueState(ValueState.None);
		},

		// ----- Scan = resolve the doff and add it to the list ----------------

		onScan: function () {
			var oModel = this.getView().getModel("form");
			var d = oModel.getData();

			if (d.busy) { return; }                       // a lookup is already in flight
			var sTo = (d.toSloc || "").trim();
			if (!sTo) {
				this.byId("inpToSloc").setValueState(ValueState.Error);
				MessageToast.show(this._t("errNoDest"));
				return;
			}
			var sQr = (d.scanText || "").trim();
			if (!sQr) {
				MessageToast.show(this._t("errScanEmpty"));
				return;
			}
			if (!d.sapOnline) {
				MessageBox.error(this._t("sapOfflineMsg"));
				return;
			}
			this._doResolve(sQr);
		},

		_doResolve: function (sQr) {
			var oModel = this.getView().getModel("form");
			oModel.setProperty("/busy", true);

			fetch("api/stock/resolve", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ qr: sQr })
			})
				.then(function (res) {
					return res.json().then(function (body) {
						return { ok: res.ok, body: body };
					});
				})
				.then(function (r) {
					oModel.setProperty("/busy", false);
					if (!r.ok) {
						MessageBox.warning(r.body && r.body.error ? r.body.error : this._t("errResolveTitle"),
							{ title: this._t("errResolveTitle") });
						return;
					}
					this._addLines(r.body.lines || []);
					var aDef = r.body.deficits || [];
					if (aDef.length) {
						MessageBox.warning(aDef.join("\n"), { title: this._t("errDeficitTitle") });
					}
					oModel.setProperty("/scanText", "");
					var oScan = this.byId("inpScan");
					if (oScan) { oScan.focus(); }
				}.bind(this))
				.catch(function () {
					oModel.setProperty("/busy", false);
					MessageBox.error(this._t("errNetwork"));
				}.bind(this));
		},

		/** Append resolved doff line(s) to the cart, skipping batches already in it. */
		_addLines: function (aLines) {
			var oModel = this.getView().getModel("form");
			var aCart = oModel.getProperty("/cart").slice();
			var iAdded = 0, sDup = "";

			aLines.forEach(function (ln) {
				var bExists = aCart.some(function (c) { return c.batch === ln.batch; });
				if (bExists) { sDup = ln.batch; return; }
				aCart.push({
					plant: ln.plant, sloc: ln.sloc, material: ln.material, batch: ln.batch,
					uom: ln.uom, doffLength: ln.doffLength, doffBatchNo: ln.doffBatchNo,
					article: ln.article, qty: ln.doffLength   // default = full doff length, editable
				});
				iAdded++;
				MessageToast.show(this._t("addedLine", [ln.batch, ln.doffLength, ln.uom]));
			}.bind(this));

			oModel.setProperty("/cart", aCart);
			if (iAdded === 0 && sDup) {
				MessageToast.show(this._t("dupLine", [sDup]));
			}
		},

		/** Live length validation: numeric, > 0 and not more than the doff length. */
		onLengthChange: function (oEvent) {
			var oInput = oEvent.getSource();
			var oLine = oInput.getBindingContext("form").getObject();
			var sVal = (oEvent.getParameter("value") || "").trim();
			var fVal = Number(sVal);
			var fMax = Number(oLine.doffLength);
			var bValid = sVal !== "" && isFinite(fVal) && fVal > 0 &&
				(!isFinite(fMax) || fVal <= fMax);
			oInput.setValueStateText(this._t("errLenInvalid"));
			oInput.setValueState(bValid ? ValueState.None : ValueState.Error);
		},

		onRemoveLine: function (oEvent) {
			var oModel = this.getView().getModel("form");
			var sPath = oEvent.getSource().getBindingContext("form").getPath();  // /cart/N
			var iIdx = parseInt(sPath.split("/").pop(), 10);
			var aCart = oModel.getProperty("/cart").slice();
			if (iIdx >= 0) {
				aCart.splice(iIdx, 1);
				oModel.setProperty("/cart", aCart);
			}
		},

		// ----- Post move = post the whole list as one document ---------------

		onPostMove: function () {
			var oModel = this.getView().getModel("form");
			var d = oModel.getData();

			if (d.busy) { return; }
			var sTo = (d.toSloc || "").trim();
			if (!sTo) {
				this.byId("inpToSloc").setValueState(ValueState.Error);
				MessageToast.show(this._t("errNoDest"));
				return;
			}
			var aCart = d.cart || [];
			if (!aCart.length) {
				MessageToast.show(this._t("errNoLines"));
				return;
			}
			if (!d.sapOnline) {
				MessageBox.error(this._t("sapOfflineMsg"));
				return;
			}
			var bBad = aCart.some(function (c) {
				var f = Number(c.qty), m = Number(c.doffLength);
				return !(c.qty !== "" && isFinite(f) && f > 0 && (!isFinite(m) || f <= m));
			});
			if (bBad) {
				MessageBox.error(this._t("errLenInvalid"), { title: this._t("errLenTitle") });
				return;
			}
			this._doPost(aCart, sTo, (d.operator || "").trim());
		},

		_doPost: function (aCart, sTo, sUser) {
			var oModel = this.getView().getModel("form");
			oModel.setProperty("/busy", true);

			var aItems = aCart.map(function (c) {
				return {
					plant: c.plant, sloc: c.sloc, material: c.material, batch: c.batch,
					uom: c.uom, qty: String(c.qty), doffLength: c.doffLength
				};
			});

			fetch("api/stock/move", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ items: aItems, toSloc: sTo, user: sUser })
			})
				.then(function (res) {
					return res.json().then(function (body) {
						return { ok: res.ok, body: body };
					});
				})
				.then(function (r) {
					oModel.setProperty("/busy", false);
					if (!r.ok) {
						MessageBox.error(r.body && r.body.error ? r.body.error : this._t("errMoveFailed"),
							{ title: this._t("errMoveTitle") });
						return;
					}
					this._onPosted(r.body);
				}.bind(this))
				.catch(function () {
					oModel.setProperty("/busy", false);
					MessageBox.error(this._t("errNetwork"), { title: this._t("errMoveTitle") });
				}.bind(this));
		},

		/** Record a successful posting: result panel + per-line history + clear list. */
		_onPosted: function (body) {
			var oModel = this.getView().getModel("form");
			var aMoved = body.moved || [];
			var sSummary = this._t("movedSummary", [aMoved.length, body.toSloc, body.matdoc]);

			oModel.setProperty("/result", {
				matdoc: body.matdoc, year: body.year, toSloc: body.toSloc,
				plant: (aMoved[0] && aMoved[0].plant) || "",
				count: String(aMoved.length), summary: sSummary
			});
			oModel.setProperty("/hasResult", true);

			var aHist = oModel.getProperty("/history").slice();
			var sNow = this._nowText();
			aMoved.forEach(function (m) {
				aHist.unshift({
					time: sNow, matdoc: body.matdoc, material: m.material,
					batch: m.batch, qty: m.qty, uom: m.uom, sloc: m.sloc, toSloc: body.toSloc
				});
			});
			oModel.setProperty("/history", aHist);

			oModel.setProperty("/cart", []);
			MessageToast.show((body.mock ? this._t("mockPrefix") : "") + sSummary);

			var oScan = this.byId("inpScan");
			if (oScan) { oScan.focus(); }
		},

		// ----- Camera QR scan (iPad Safari) ---------------------------------

		onScanQr: function () {
			var d = this.getView().getModel("form").getData();
			if (!(d.toSloc || "").trim()) {
				this.byId("inpToSloc").setValueState(ValueState.Error);
				MessageToast.show(this._t("errNoDest"));
				return;
			}
			if (!window.jsQR) {
				MessageBox.error(this._t("errQrLib"));
				return;
			}
			if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
				MessageBox.warning(this._t("errCameraContext"));
				return;
			}

			if (!this._oScanDialog) {
				this._oScanHtml = new HTML({
					content:
						"<div style='text-align:center'>" +
						"<video id='stmQrVideo' autoplay muted playsinline " +
						"style='width:100%;max-height:60vh;background:#000;border-radius:.5rem'></video>" +
						"<canvas id='stmQrCanvas' style='display:none'></canvas>" +
						"</div>"
				});
				this._oScanDialog = new Dialog({
					title: this._t("scanQrTitle"),
					contentWidth: "32rem",
					stretchOnPhone: true,
					content: [this._oScanHtml],
					endButton: new Button({
						text: this._t("cancel"),
						press: function () { this._oScanDialog.close(); }.bind(this)
					}),
					afterClose: this._stopCamera.bind(this)
				});
				this.getView().addDependent(this._oScanDialog);
			}

			this._oScanDialog.open();
			setTimeout(this._startCamera.bind(this), 0);
		},

		_startCamera: function () {
			var video = document.getElementById("stmQrVideo");
			if (!video) { return; }
			navigator.mediaDevices.getUserMedia({
				audio: false,
				video: { facingMode: { ideal: "environment" } }
			}).then(function (stream) {
				this._mediaStream = stream;
				video.setAttribute("playsinline", "");
				video.srcObject = stream;
				var play = video.play();
				if (play && play.catch) { play.catch(function () {}); }
				this._scanning = true;
				this._decodeTick();
			}.bind(this)).catch(function (err) {
				this._oScanDialog.close();
				var msg = (err && (err.name === "NotAllowedError" || err.name === "SecurityError"))
					? this._t("errCameraDenied") : this._t("errCameraContext");
				MessageBox.warning(msg);
			}.bind(this));
		},

		_decodeTick: function () {
			if (!this._scanning) { return; }
			var video = document.getElementById("stmQrVideo");
			var canvas = document.getElementById("stmQrCanvas");
			if (!video || !canvas || video.readyState !== video.HAVE_ENOUGH_DATA) {
				this._rafId = window.requestAnimationFrame(this._decodeTick.bind(this));
				return;
			}
			var w = video.videoWidth, h = video.videoHeight;
			canvas.width = w;
			canvas.height = h;
			var ctx = canvas.getContext("2d");
			ctx.drawImage(video, 0, 0, w, h);
			var img = ctx.getImageData(0, 0, w, h);
			var code = window.jsQR(img.data, w, h, { inversionAttempts: "dontInvert" });
			if (code && code.data) {
				this._scanning = false;
				var sValue = String(code.data).trim();
				this.getView().getModel("form").setProperty("/scanText", sValue);
				this._oScanDialog.close();
				this.onScan();               // decode -> resolve + add to the list
				return;
			}
			this._rafId = window.requestAnimationFrame(this._decodeTick.bind(this));
		},

		_stopCamera: function () {
			this._scanning = false;
			if (this._rafId) {
				window.cancelAnimationFrame(this._rafId);
				this._rafId = null;
			}
			if (this._mediaStream) {
				this._mediaStream.getTracks().forEach(function (t) { t.stop(); });
				this._mediaStream = null;
			}
			var video = document.getElementById("stmQrVideo");
			if (video) { video.srcObject = null; }
		},

		// ----- Reset ---------------------------------------------------------

		onReset: function () {
			this._stopCamera();
			this.getView().setModel(this._newModel(), "form");
			var oScan = this.byId("inpScan");
			if (oScan) {
				oScan.setValueState(ValueState.None);
				oScan.focus();
			}
		},

		// ----- Helpers -------------------------------------------------------

		_pad: function (n) { return (n < 10 ? "0" : "") + n; },

		_nowText: function () {
			var d = new Date();
			return this._pad(d.getHours()) + ":" + this._pad(d.getMinutes()) + ":" + this._pad(d.getSeconds());
		},

		_t: function (sKey, aArgs) {
			return this.getOwnerComponent().getModel("i18n")
				.getResourceBundle().getText(sKey, aArgs);
		}
	});
});
