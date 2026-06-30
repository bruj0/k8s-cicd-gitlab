$(document).ready(function() {
  var headerTitleElement = $("#header h1");
  var entriesElement = $("#guestbook-entries");
  var formElement = $("#guestbook-form");
  var submitElement = $("#guestbook-submit");
  var entryContentElement = $("#guestbook-entry-content");
  var hostAddressElement = $("#guestbook-host-address");
  var envVariableElement1 = $("#environment-variable1");
  var envVariableElement2 = $("#environment-variable2");
  var envVariableElement3 = $("#environment-variable3");
  var envVariableElement4 = $("#environment-variable4");
  var myVersionElement = $("#my-version");

  var appendGuestbookEntries = function(data) {
    entriesElement.empty();
    $.each(data, function(key, val) {
      entriesElement.append("<p>" + val + "</p>");
    });
  }

  var handleSubmission = function(e) {
    e.preventDefault();
    var entryValue = entryContentElement.val()
    if (entryValue.length > 0) {
      entriesElement.append("<p>...</p>");
      $.getJSON("rpush/guestbook/" + entryValue, appendGuestbookEntries);
    }
    return false;
  }

  // colors = purple, blue, red, green, yellow
  var colors = ["#549", "#18d", "#d31", "#2a4", "#db1"];
  var randomColor = colors[Math.floor(5 * Math.random())];
  (function setElementsColor(color) {
    headerTitleElement.css("color", color);
    entryContentElement.css("box-shadow", "inset 0 0 0 2px " + color);
    submitElement.css("background-color", color);
  })(randomColor);

  submitElement.click(handleSubmission);
  formElement.submit(handleSubmission);
  hostAddressElement.append(document.URL);

  // Load environment variable (e.g., "APP_VERSION")
  function loadEnvironmentVariable(envVariableElement,envVarName) {
    $.getJSON("env/" + envVarName)
      .done(function(data) {
        envVariableElement.text(envVarName + ": " + data.value);
      })
      .fail(function() {
        envVariableElement.text("Could not load environment variable: " + envVarName);
      });
  }

  function loadEnvironmentVariable2(envVariableElement,envVarName) {
    $.getJSON("env/" + envVarName)
      .done(function(data) {
        envVariableElement.text(data.value);
      })
      .fail(function() {
        envVariableElement.text("Could not load environment variable: " + envVarName);
      });
  }
  // Load environment variable when page loads
  loadEnvironmentVariable(envVariableElement1, "ENVIRONMENT");
  loadEnvironmentVariable(envVariableElement2, "SOURCE_VALUE");
  loadEnvironmentVariable(envVariableElement3, "COMMON_TEST_VALUE1");
  loadEnvironmentVariable(envVariableElement4, "COMMON_TEST_VALUE2");
  loadEnvironmentVariable2(myVersionElement, "APP_VERSION");



  // Poll every second.
  (function fetchGuestbook() {
    $.getJSON("lrange/guestbook").done(appendGuestbookEntries).always(
      function() {
        setTimeout(fetchGuestbook, 1000);
      });
  })();
});
