sap.ui.define([
	"sap/ui/core/mvc/Controller",
	"sap/ui/model/json/JSONModel",
	"sap/ui/core/library",
	"sap/m/MessageToast",
	"sap/m/MessageBox",
	"sap/m/Dialog",
	"sap/m/Button",
	"sap/ui/core/Fragment",
	"sap/ui/core/HTML"
], function (Controller, JSONModel, coreLibrary, MessageToast, MessageBox, Dialog, Button, Fragment, HTML) {
	"use strict";

	var ValueState = coreLibrary.ValueState;

	return Controller.extend("stock.transfer.controller.StockMove", {

		onInit: function () {
			this.getView().setModel(this._newModel(), "form");
			this._checkHealth();
			this._healthTimer = setInterval(this._checkHealth.bind(this), 20000);
			this._checkAuth();       // gate the app behind a SAP login
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
				operator: "",            // set from the SAP login (no manual entry)
				fullName: "",
				isAdmin: false,
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

		// ----- SAP login (8-hour session) -----------------------------------

		/** On load, ask the server who is signed in; open the login if nobody is. */
		_checkAuth: function () {
			fetch("api/auth/me")
				.then(function (res) {
					if (!res.ok) { throw new Error("noauth"); }
					return res.json();
				})
				.then(function (b) {
					this._applyLogin(b, true);
				}.bind(this))
				.catch(function () { this._openLogin(); }.bind(this));
		},

		_openLogin: function () {
			var oView = this.getView();
			if (!this._pLogin) {
				this._pLogin = Fragment.load({
					id: oView.getId(),
					name: "stock.transfer.view.Login",
					controller: this
				}).then(function (oDialog) {
					oView.addDependent(oDialog);
					return oDialog;
				});
			}
			this._pLogin.then(function (oDialog) {
				var oPass = this.byId("loginPass");
				if (oPass) { oPass.setValue(""); oPass.setValueState(ValueState.None); }
				if (!oDialog.isOpen()) { oDialog.open(); }
			}.bind(this));
		},

		/** Block ESC / outside-tap: the login can only be dismissed by signing in. */
		onLoginEscape: function (oPromise) {
			oPromise.reject();
		},

		/** Show/hide the password field. */
		onTogglePass: function () {
			var oPass = this.byId("loginPass"), oEye = this.byId("loginEye");
			var bHidden = oPass.getType() === "Password";
			oPass.setType(bHidden ? "Text" : "Password");
			oEye.setSrc(bHidden ? "sap-icon://hide" : "sap-icon://show");
		},

		onLoginSubmit: function () {
			var oUser = this.byId("loginUser"), oPass = this.byId("loginPass");
			var sUser = (oUser.getValue() || "").trim();
			var sPass = oPass.getValue() || "";
			if (!sUser || !sPass) {
				MessageToast.show(this._t("loginNeed"));
				return;
			}
			var oBtn = this.byId("loginBtn");
			oBtn.setBusy(true);
			fetch("api/auth/login", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ user: sUser, password: sPass })
			})
				.then(function (res) {
					return res.json().then(function (body) { return { ok: res.ok, body: body }; });
				})
				.then(function (r) {
					oBtn.setBusy(false);
					if (!r.ok) {
						oPass.setValueState(ValueState.Error);
						MessageBox.error(r.body && r.body.error ? r.body.error : this._t("loginFailed"),
							{ title: this._t("loginTitle") });
						return;
					}
					this._applyLogin(r.body);
				}.bind(this))
				.catch(function () {
					oBtn.setBusy(false);
					MessageBox.error(this._t("errNetwork"));
				}.bind(this));
		},

		/** Apply a successful sign-in (password, QR, or /me restore). */
		_applyLogin: function (body, bSilent) {
			var oModel = this.getView().getModel("form");
			oModel.setProperty("/operator", body.user || "");
			oModel.setProperty("/fullName", body.fullName || "");
			oModel.setProperty("/isAdmin", !!body.isAdmin);
			var oLogin = this.byId("loginDialog");
			if (oLogin && oLogin.isOpen()) { oLogin.close(); }
			if (!bSilent) {
				MessageToast.show(this._t("welcome", [body.fullName || body.user]));
				var oScan = this.byId("inpScan");
				if (oScan) { oScan.focus(); }
			}
		},

		/** Badge (QR) sign-in: scan a badge from the login screen. */
		onScanBadge: function () {
			this._openCamera("badge");
		},

		_doQrLogin: function (sToken) {
			fetch("api/auth/login-qr", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ qr: sToken })
			})
				.then(function (res) {
					return res.json().then(function (body) { return { ok: res.ok, body: body }; });
				})
				.then(function (r) {
					if (!r.ok) {
						MessageBox.error(r.body && r.body.error ? r.body.error : this._t("loginFailed"),
							{ title: this._t("loginTitle") });
						return;
					}
					this._applyLogin(r.body);
				}.bind(this))
				.catch(function () { MessageBox.error(this._t("errNetwork")); }.bind(this));
		},

		onLogout: function () {
			fetch("api/auth/logout", { method: "POST" })
				.then(function () {
					var oModel = this.getView().getModel("form");
					oModel.setProperty("/operator", "");
					oModel.setProperty("/fullName", "");
					oModel.setProperty("/isAdmin", false);
					oModel.setProperty("/cart", []);
					oModel.setProperty("/history", []);
					this._openLogin();
				}.bind(this))
				.catch(function () { this._openLogin(); }.bind(this));
		},

		/** Session gone (server returned 401): drop back to the login. */
		_sessionExpired: function () {
			this.getView().getModel("form").setProperty("/busy", false);
			MessageToast.show(this._t("sessionExpired"));
			this._openLogin();
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
					var iStatus = res.status;
					return res.json().then(function (body) {
						return { ok: res.ok, status: iStatus, body: body };
					});
				})
				.then(function (r) {
					oModel.setProperty("/busy", false);
					if (r.status === 401) { this._sessionExpired(); return; }
					if (!r.ok) {
						MessageBox.warning(r.body && r.body.error ? r.body.error : this._t("errResolveTitle"),
							{ title: this._t("errResolveTitle") });
						return;
					}
					this._addLines(r.body.lines || [], r.body.parsed || {});
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

		/** Append resolved doff line(s) to the cart, skipping batches already in it.
		 *  oParsed carries the beam context (lot/loom/beam/seq/raw) for the save. */
		_addLines: function (aLines, oParsed) {
			var oModel = this.getView().getModel("form");
			var aCart = oModel.getProperty("/cart").slice();
			var p = oParsed || {};
			var iAdded = 0, sDup = "";

			aLines.forEach(function (ln) {
				var bExists = aCart.some(function (c) { return c.batch === ln.batch; });
				if (bExists) { sDup = ln.batch; return; }
				aCart.push({
					plant: ln.plant, sloc: ln.sloc, material: ln.material, batch: ln.batch,
					uom: ln.uom, doffLength: ln.doffLength, doffBatchNo: ln.doffBatchNo,
					article: ln.article, qty: ln.doffLength,   // default = full doff length, editable
					lot: p.lot || "", loom: p.loom || "", beam: p.beam || "",
					seq: p.seq || "", qrRaw: p.raw || ""
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

		// ----- Save = record the list as "Doff in Transit" (no 311) ----------

		onSaveTransit: function () {
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
			var bBad = aCart.some(function (c) {
				var f = Number(c.qty), m = Number(c.doffLength);
				return !(c.qty !== "" && isFinite(f) && f > 0 && (!isFinite(m) || f <= m));
			});
			if (bBad) {
				MessageBox.error(this._t("errLenInvalid"), { title: this._t("errLenTitle") });
				return;
			}
			this._doSaveTransit(aCart, sTo);
		},

		_doSaveTransit: function (aCart, sTo) {
			var oModel = this.getView().getModel("form");
			oModel.setProperty("/busy", true);

			var aItems = aCart.map(function (c) {
				return {
					plant: c.plant, sloc: c.sloc, material: c.material, batch: c.batch,
					uom: c.uom, qty: String(c.qty), doffLength: c.doffLength,
					doffBatchNo: c.doffBatchNo, article: c.article,
					qrRaw: c.qrRaw, lot: c.lot, loom: c.loom, beam: c.beam, seq: c.seq
				};
			});

			fetch("api/stock/transit", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ items: aItems, toSloc: sTo })
			})
				.then(function (res) {
					var iStatus = res.status;
					return res.json().then(function (body) { return { ok: res.ok, status: iStatus, body: body }; });
				})
				.then(function (r) {
					oModel.setProperty("/busy", false);
					if (r.status === 401) { this._sessionExpired(); return; }
					if (!r.ok) {
						MessageBox.error(r.body && r.body.error ? r.body.error : this._t("errSaveFailed"),
							{ title: this._t("errSaveTitle") });
						return;
					}
					this._onSaved(r.body);
				}.bind(this))
				.catch(function () {
					oModel.setProperty("/busy", false);
					MessageBox.error(this._t("errNetwork"), { title: this._t("errSaveTitle") });
				}.bind(this));
		},

		/** Record a successful save: per-line transit history + clear the list. */
		_onSaved: function (body) {
			var oModel = this.getView().getModel("form");
			var aSaved = body.saved || [];
			var sSummary = this._t("savedSummary", [aSaved.length, body.toSloc]);

			var aHist = oModel.getProperty("/history").slice();
			var sNow = this._nowText();
			aSaved.forEach(function (s) {
				aHist.unshift({
					time: sNow, matdoc: s.docid, material: s.material, batch: s.batch,
					qty: s.qty, uom: s.uom, sloc: s.sloc, toSloc: body.toSloc,
					status: s.status || "Doff in Transit"
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
			this._openCamera("doff");
		},

		/** Open the camera for a doff scan ("doff") or a badge login ("badge"). */
		_openCamera: function (sTarget) {
			this._camTarget = sTarget;
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

			this._oScanDialog.setTitle(sTarget === "badge" ? this._t("scanBadgeTitle") : this._t("scanQrTitle"));
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
				this._oScanDialog.close();
				if (this._camTarget === "badge") {
					this._doQrLogin(sValue);          // badge scan -> QR login
				} else {
					this.getView().getModel("form").setProperty("/scanText", sValue);
					this.onScan();                    // doff scan -> resolve + add
				}
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

		// ----- Admin: login badges (admins only) ----------------------------

		onOpenAdmin: function () {
			var oView = this.getView();
			if (!oView.getModel("admin")) {
				oView.setModel(new JSONModel({ badges: [], newUser: "", newName: "" }), "admin");
			}
			if (!this._pAdmin) {
				this._pAdmin = Fragment.load({
					id: oView.getId(), name: "stock.transfer.view.Admin", controller: this
				}).then(function (oDlg) { oView.addDependent(oDlg); return oDlg; });
			}
			this._pAdmin.then(function (oDlg) { this._loadBadges(); oDlg.open(); }.bind(this));
		},

		_loadBadges: function () {
			fetch("api/admin/qr")
				.then(function (res) { return res.json().then(function (b) { return { ok: res.ok, body: b }; }); })
				.then(function (r) {
					if (r.ok) { this.getView().getModel("admin").setProperty("/badges", r.body.badges || []); }
					else { MessageBox.error(r.body && r.body.error ? r.body.error : this._t("adminLoadFail")); }
				}.bind(this))
				.catch(function () { MessageBox.error(this._t("errNetwork")); }.bind(this));
		},

		onAdminClose: function () {
			var oDlg = this.byId("adminDialog");
			if (oDlg) { oDlg.close(); }
		},

		onAdminCreate: function () {
			var oModel = this.getView().getModel("admin");
			var sUser = (oModel.getProperty("/newUser") || "").trim();
			var sName = (oModel.getProperty("/newName") || "").trim();
			if (!sUser) { MessageToast.show(this._t("adminNeedUser")); return; }
			fetch("api/admin/qr", {
				method: "POST", headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ sapUser: sUser, fullName: sName })
			})
				.then(function (res) { return res.json().then(function (b) { return { ok: res.ok, body: b }; }); })
				.then(function (r) {
					if (!r.ok) { MessageBox.error(r.body && r.body.error ? r.body.error : this._t("adminAddFail")); return; }
					oModel.setProperty("/newUser", "");
					oModel.setProperty("/newName", "");
					this._loadBadges();
					this._printBadge(r.body.token, r.body.sapUser, r.body.fullName);
				}.bind(this))
				.catch(function () { MessageBox.error(this._t("errNetwork")); }.bind(this));
		},

		onAdminToggle: function (oEvent) {
			var o = oEvent.getSource().getBindingContext("admin").getObject();
			fetch("api/admin/qr/toggle", {
				method: "POST", headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ qrId: o.qrId, active: o.active !== "X" })
			})
				.then(function (res) { return res.json().then(function (b) { return { ok: res.ok, body: b }; }); })
				.then(function (r) {
					if (r.ok) { this._loadBadges(); }
					else { MessageBox.error(r.body && r.body.error ? r.body.error : this._t("adminAddFail")); }
				}.bind(this))
				.catch(function () { MessageBox.error(this._t("errNetwork")); }.bind(this));
		},

		onPrintBadge: function (oEvent) {
			var o = oEvent.getSource().getBindingContext("admin").getObject();
			this._printBadge(o.token, o.sapUser, o.fullName);
		},

		/** Render a printable badge card (QR + name) in a new window and print it. */
		_printBadge: function (sToken, sUser, sName) {
			if (!window.qrcode) { MessageBox.error(this._t("errQrLib")); return; }
			var esc = function (s) {
				return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
					return { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;" }[c];
				});
			};
			var qr = window.qrcode(0, "M");
			qr.addData(sToken);
			qr.make();
			var sImg = qr.createDataURL(6, 4);
			var sHtml =
				"<!DOCTYPE html><html><head><meta charset='utf-8'><title>Badge " + esc(sUser) + "</title><style>" +
				"body{font-family:-apple-system,'Segoe UI',Arial,sans-serif;margin:0;padding:24px;display:flex;justify-content:center}" +
				".card{width:230px;border:1px solid #d7dae0;border-radius:14px;overflow:hidden;text-align:center}" +
				".hd{background:#1f3d6b;color:#fff;font-size:12px;font-weight:600;letter-spacing:.5px;padding:10px}" +
				".bd{padding:16px}.bd img{width:150px;height:150px;image-rendering:pixelated}" +
				".u{font-size:16px;font-weight:600;margin-top:8px}.n{font-size:12px;color:#666}.s{font-size:10px;color:#999;margin-top:4px}" +
				"</style></head><body onload=\"setTimeout(function(){window.print();},250)\">" +
				"<div class='card'><div class='hd'>KASSIM &middot; STOCK MOVEMENT</div><div class='bd'>" +
				"<img src='" + sImg + "'><div class='u'>" + esc(sUser) + "</div><div class='n'>" + esc(sName) + "</div>" +
				"<div class='s'>Scan to sign in</div></div></div></body></html>";
			var w = window.open("", "_blank");
			if (!w) { MessageBox.warning(this._t("adminPopup")); return; }
			w.document.open();
			w.document.write(sHtml);
			w.document.close();
		},

		// ----- Reset ---------------------------------------------------------

		onReset: function () {
			this._stopCamera();
			this.getView().setModel(this._newModel(), "form");
			this._checkAuth();       // keep the signed-in operator after a reset
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
