cmake_minimum_required(VERSION 3.22.1)
project(ftp_contract_tests LANGUAGES CXX)

# Only the FTP plugins are needed for this local source-native test target.
# The complete official native/server gates remain separate validation.
set(SUPERBUILD OFF CACHE BOOL "Use the retained dependency install read-only" FORCE)
set(BUILD_TESTING OFF CACHE BOOL "Build the focused test runner below" FORCE)
set(BUILD_MAVSDK_SERVER OFF CACHE BOOL "Local native client validation" FORCE)
set(BUILD_WITHOUT_CURL ON CACHE BOOL "Match the retained dependency profile" FORCE)
set(ENABLED_PLUGINS "ftp;ftp_server" CACHE STRING "Focused plugins" FORCE)
set(WERROR ON CACHE BOOL "Treat library warnings as errors" FORCE)
add_subdirectory("${MAVSDK_SOURCE}/cpp" mavsdk)

find_package(GTest REQUIRED)
find_package(asio REQUIRED)
add_executable(ftp_result_tests "${TEST_SOURCE}/ftp_malformed_nak.cpp")
if(PROFILE_TESTS)
    target_sources(ftp_result_tests PRIVATE "${TEST_SOURCE}/ftp_server_errno.cpp")
endif()
target_compile_features(ftp_result_tests PRIVATE cxx_std_20)
get_directory_property(mavsdk_test_warnings
    DIRECTORY "${MAVSDK_SOURCE}/cpp/src" DEFINITION warnings)
set_target_properties(ftp_result_tests PROPERTIES COMPILE_FLAGS "${mavsdk_test_warnings}")
target_include_directories(ftp_result_tests PRIVATE
    "${MAVSDK_SOURCE}/cpp/src/mavsdk/core"
    "${TEST_SOURCE}"
)
target_link_libraries(ftp_result_tests PRIVATE mavsdk GTest::gtest_main asio::asio)
